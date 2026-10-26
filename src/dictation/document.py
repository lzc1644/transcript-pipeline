"""Continuous notebook operations. All positions are UTF-16 offsets, like CodeMirror.

Only disjoint concurrent edits are rebased. Intersections are surfaced to the
editor, never resolved by searching/replacing matching sentence text.
"""
from __future__ import annotations

from uuid import uuid4

from src.dictation.scheduler import Conflict

HISTORY_LIMIT = 1000


def length(text):
    return len(text.encode("utf-16-le")) // 2


def index(text, position):
    if type(position) is not int or not 0 <= position <= length(text):
        raise ValueError("文字位置超出正文")
    try:
        return len(text.encode("utf-16-le")[:position * 2].decode("utf-16-le"))
    except UnicodeDecodeError:
        raise ValueError("文字位置不能切开 emoji 等字符") from None


def map_position(position, changes, assoc=1):
    offset = 0
    for c in changes:
        start, end, size = c["from"], c["to"], length(c["insert"])
        if position < start:
            break
        if position > end:
            offset += size - (end - start)
        else:
            return start + offset + (size if assoc > 0 else 0)
    return position + offset


def intersects(a, b):
    # Insertions at the edge of a replacement are safe; inside it they aren't.
    if a["from"] == a["to"]:
        return b["from"] < a["from"] < b["to"]
    if b["from"] == b["to"]:
        return a["from"] < b["from"] < a["to"]
    return max(a["from"], b["from"]) < min(a["to"], b["to"])


def rebase(changes, over):
    if any(intersects(a, b) for a in changes for b in over):
        raise Conflict("另一窗口或后台修改了同一范围；本地编辑已保留，请处理冲突")
    return [{**c, "from": map_position(c["from"], over, 1),
             "to": map_position(c["to"], over, 1 if c["from"] == c["to"] else -1)} for c in changes]


def enable(state, render):
    if state.get("editor_version") == 1:
        return
    text = render(state)
    state.update(editor_version=1, document_text=text, document_version=0,
                 document_operations=[], document_sources=[], dictation_turns=[])
    # Historical line breaks and applied whole drafts are preserved verbatim.
    if not state.get("document_revision"):
        position = 0
        for u in state["utterances"]:
            if u.get("raw_text") or u.get("edited"):
                state["document_sources"].append({"utterance_id": u["id"], "from": position,
                    "to": position + length(u["text"]), "touched": bool(u.get("edited"))})
                position += length(u["text"]) + 1
    if state["status"] in {"created", "preparing", "listening", "pausing", "paused", "draining", "waiting"}:
        start_turn(state, 0, length(text))


def start_turn(state, samples, position):
    index(state["document_text"], position)
    turns = [dict(t) for t in state["dictation_turns"]]
    if turns:
        turns[-1]["end_sample"] = samples
    state["dictation_turns"] = turns + [{"id": len(turns) + 1, "start_sample": samples, "position": position}]


def apply(state, changes, *, operation_id=None, kind="user", utterance_id=None):
    text = state["document_text"]
    previous_end = 0
    for c in changes:
        if c["from"] < previous_end or c["to"] < c["from"]:
            raise ValueError("文字操作范围重叠或顺序无效")
        index(text, c["from"])
        index(text, c["to"])
        previous_end = c["to"]
    candidate_text = text
    for c in reversed(changes):
        candidate_text = candidate_text[:index(candidate_text, c["from"])] + c["insert"] + candidate_text[index(candidate_text, c["to"]):]
    if length(candidate_text) > 200000:
        raise ValueError("正文超过20万 UTF-16 字符，请导出后新建记录")
    sources = [dict(s) for s in state["document_sources"]]
    turns = [dict(t) for t in state["dictation_turns"]]
    for source in sources:
        span = {"from": source["from"], "to": source["to"]}
        # A new dictation turn may insert inside an older sentence too. Its
        # delayed cleanup must not replace that larger, now mixed-source span.
        if kind in {"user", "apply", "speech"} and any(intersects(span, c) for c in changes):
            source["touched"] = True
        source["from"] = map_position(source["from"], changes, 1)
        source["to"] = max(source["from"], map_position(source["to"], changes, -1))
    for turn in turns:
        turn["position"] = map_position(turn["position"], changes, 1)
    version = state["document_version"] + 1
    op = {"id": operation_id or uuid4().hex, "version": version,
          "changes": changes, "kind": kind, "utterance_id": utterance_id}
    state.update(document_text=candidate_text, document_sources=sources, dictation_turns=turns,
                 document_version=version, document_operations=(state["document_operations"] + [op])[-HISTORY_LIMIT:])
    return op


def edit(state, operation_id, version, changes):
    history = state["document_operations"]
    if any(op["id"] == operation_id for op in history):
        return False
    if version > state["document_version"] or version < (history[0]["version"] - 1 if history else 0):
        raise Conflict("正文同步版本过旧；本地编辑已保留，请重新载入后合并")
    for op in history:
        if op["version"] > version:
            changes = rebase(changes, op["changes"])
    apply(state, changes, operation_id=operation_id)
    return True


def insert_utterance(state, u):
    if any(s["utterance_id"] == u["id"] for s in state["document_sources"]):
        return
    turns = state["dictation_turns"]
    turn = next((t for t in reversed(turns) if t["start_sample"] <= u["start_sample"]), None)
    position = turn["position"] if turn else length(state["document_text"])
    apply(state, [{"from": position, "to": position, "insert": u["text"]}], kind="speech", utterance_id=u["id"])
    state["document_sources"].append({"utterance_id": u["id"], "from": position,
        "to": position + length(u["text"]), "touched": bool(u.get("edited"))})


def clean_utterance(state, u, text):
    source = next((s for s in state["document_sources"] if s["utterance_id"] == u["id"]), None)
    if not source or source["touched"] or source["from"] == source["to"]:
        return False
    start, end = source["from"], source["to"]
    apply(state, [{"from": start, "to": end, "insert": text}], kind="cleanup", utterance_id=u["id"])
    next(s for s in state["document_sources"] if s["utterance_id"] == u["id"]).update(
        {"from": start, "to": start + length(text)})
    return True
