from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import wave

ACTIVE = {"created", "waiting", "preparing", "listening", "pausing", "paused", "draining"}


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class Storage:
    def __init__(self, root: Path):
        self.root = root / "data/dictation"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, sid):
        if not re.fullmatch(r"[a-f0-9]{32}", sid):
            raise KeyError("会话不存在")
        path = self.root / sid
        if path.is_symlink():
            raise KeyError("会话不存在")
        return path

    def create(self, state):
        directory = self.path(state["id"])
        directory.mkdir(mode=0o700)
        for name in ("audio", "asr", "cleaned"):
            (directory / name).mkdir()
        (directory / "audio/source.pcm").touch()
        self.save(state)

    def load(self, sid):
        try:
            return json.loads((self.path(sid) / "session.json").read_text())
        except (FileNotFoundError, ValueError):
            raise KeyError("会话不存在") from None

    def save(self, state):
        directory = self.path(state["id"])
        atomic_json(directory / "session.json", state)
        text = self.render_draft(state)
        tmp = directory / "draft.txt.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, directory / "draft.txt")
        atomic_json(directory / "metrics.json", state.get("metrics", {}))

    @staticmethod
    def render_draft(state):
        # An explicitly applied/edited whole draft remains authoritative until
        # an explicit return to utterance editing. Background cleanup cannot
        # silently revoke it.
        if state.get("document_text") is not None:
            return state["document_text"]
        return "\n".join(u["text"] for u in state["utterances"] if u.get("raw_text") or u.get("edited"))

    def record(self, sid, event):
        with (self.path(sid) / "utterances.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

    def artifact(self, sid, kind, uid, value):
        if not re.fullmatch(r"(?:[0-9]+|document-[a-f0-9]{32})", uid):
            raise ValueError("语句编号无效")
        atomic_json(self.path(sid) / kind / f"{uid}.json", value)

    def append_audio(self, sid, pcm):
        with (self.path(sid) / "audio/source.pcm").open("ab") as output:
            output.write(pcm)
            output.flush()
            os.fsync(output.fileno())

    def same_audio(self, sid, start, pcm):
        with (self.path(sid) / "audio/source.pcm").open("rb") as audio:
            audio.seek(start * 2)
            return audio.read(len(pcm)) == pcm

    def utterance_audio(self, sid, uid, start, end):
        path = self.path(sid) / "audio" / f"{uid}.wav"
        with (self.path(sid) / "audio/source.pcm").open("rb") as source, wave.open(str(path), "wb") as output:
            source.seek(start * 2)
            pcm = source.read((end - start) * 2)
            if len(pcm) != (end - start) * 2:
                raise ValueError("音频未完整持久化")
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
        return path

    def all(self):
        for path in self.root.glob("*/session.json"):
            if path.parent.is_symlink():
                continue
            try:
                yield json.loads(path.read_text())
            except (OSError, ValueError):
                continue

    def recover(self):
        for state in self.all():
            if state["status"] in ACTIVE:
                state.update(status="interrupted", phase="服务重启，录音已中断", error="服务重启；原始音频和已完成文字保留",
                             revision=state["revision"] + 1, event_sequence=state["event_sequence"] + 1,
                             updated_at=datetime.now(timezone.utc).isoformat())
                for u in state["utterances"]:
                    if u["status"] in {"partial", "finalizing"}:
                        u.update(status="asr_failed", error="服务重启，最终识别未完成", revision=u["revision"] + 1)
                    elif u["status"] == "cleaning":
                        u.update(status="cleanup_failed", error="服务重启，整理未完成；可重试", revision=u["revision"] + 1)
                self.save(state)

    def delete(self, sid):
        shutil.rmtree(self.path(sid))


def public_state(state):
    return {**{key: value for key, value in state.items() if key not in
               {"owner", "worker_config", "gateway", "packets"}},
            "draft_text": Storage.render_draft(state),
            "progress": {"total_samples": state.get("audio_total_samples"),
                         "transcribed_samples": state.get("asr_processed_samples", 0),
                         "asr_failed": sum(u["status"] == "asr_failed" for u in state["utterances"]),
                         "cleanup_total": sum(bool(u.get("raw_text")) for u in state["utterances"]) if state.get("config", {}).get("cleanup") else 0,
                         "cleanup_done": sum(u["status"] in {"cleaned", "cleanup_failed"} for u in state["utterances"]),
                         "cleanup_failed": sum(u["status"] == "cleanup_failed" for u in state["utterances"])}}
