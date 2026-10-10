"""Independent, opt-in proofreading memory experiment; never imports pipeline business logic."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.ocr_page_markers import is_ocr_page_break_line

PROMPTS = Path(__file__).resolve().parents[1] / "config/prompts"
SCOPE_KEYS = {"book_id", "speaker_id", "content_type"}
KINDS = {"term", "correction_case", "reading_preference", "preservation_case"}
DATASETS = {"synthetic", "real_human"}


class MemoryExperimentError(RuntimeError):
    """Invalid experiment data or operation; no implicit recovery or promotion."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MemoryExperimentError(message)


def fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    require(isinstance(value, dict), "expected JSON object")
    require(required <= value.keys() and value.keys() <= required | optional,
            f"invalid fields: required={sorted(required)}, received={sorted(value)}")


def string(value: Any, name: str, maximum: int) -> None:
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            f"{name} must be nonempty text <= {maximum} characters")


def integer(value: Any, name: str, minimum: int = 0) -> None:
    require(type(value) is int and value >= minimum, f"{name} must be integer >= {minimum}")


def parse_json(text: str) -> Any:
    require(len(text.encode("utf-8")) <= 1024 * 1024, "JSON exceeds 1 MiB")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, TypeError) as exc:
        raise MemoryExperimentError("invalid JSON") from exc


def read_json(path: str | Path) -> Any:
    try:
        p = Path(path)
        require(p.is_file(), "JSON path must be an existing file")
        require(p.stat().st_size <= 1024 * 1024, "JSON exceeds 1 MiB")
        return parse_json(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise MemoryExperimentError("cannot read UTF-8 JSON") from exc


@dataclass(frozen=True)
class AnalysisLimits:
    max_bytes: int = 1024 * 1024
    max_chars: int = 100_000
    max_lines: int = 4000
    max_fragments: int = 40
    fragment_chars: int = 600
    payload_chars: int = 40_000


def validate_scope(scope: Any, *, complete: bool) -> bool:
    require(isinstance(scope, dict) and scope.keys() <= SCOPE_KEYS, "invalid scope fields")
    for key, value in scope.items():
        string(value, key, 200)
    if "content_type" in scope:
        require(scope["content_type"] in {"reading", "conversation"}, "invalid content_type")
    result = scope.keys() == SCOPE_KEYS
    require(not complete or result, "complete book_id/speaker_id/content_type scope required")
    return result


def validate_metadata(metadata: Any) -> None:
    fields(metadata, {"dataset_kind", "scope"}, {"title", "chapter", "notes", "synthetic_edits", "source_hashes"})
    require(isinstance(metadata["dataset_kind"], str) and metadata["dataset_kind"] in DATASETS, "dataset_kind must explicitly be synthetic or real_human")
    validate_scope(metadata["scope"], complete=False)
    for key in ("title", "chapter", "notes"):
        if key in metadata:
            string(metadata[key], key, 1000)
    if "synthetic_edits" in metadata:
        require(metadata["dataset_kind"] == "synthetic" and isinstance(metadata["synthetic_edits"], list),
                "synthetic_edits requires synthetic dataset")
        require(len(metadata["synthetic_edits"]) <= 40, "too many synthetic_edits")
        for edit in metadata["synthetic_edits"]:
            string(edit, "synthetic edit", 300)
    if "source_hashes" in metadata:
        require(isinstance(metadata["source_hashes"], dict), "source_hashes must be object")
        for key, value in metadata["source_hashes"].items():
            string(key, "source hash name", 200)
            require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), "invalid source hash")


def _read_source(path: str | Path, role: str, limits: AnalysisLimits) -> dict:
    p = Path(path)
    require(p.is_file(), f"{role}: missing file or directory")
    require(p.suffix.lower() in ({".md", ".txt", ".json"} if role == "system" else {".md", ".txt"}),
            f"{role}: unsupported extension")
    try:
        require(p.stat().st_size <= limits.max_bytes, f"{role}: exceeds byte limit {limits.max_bytes}")
        raw = p.read_bytes()
        require(len(raw) <= limits.max_bytes, f"{role}: exceeds byte limit")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise MemoryExperimentError(f"{role}: cannot read UTF-8") from exc
    selector = "text"
    if p.suffix.lower() == ".json":
        data = parse_json(text)
        require(isinstance(data, dict) and isinstance(data.get("final_markdown"), str),
                "system JSON requires top-level string final_markdown")
        text, selector = data["final_markdown"], "final_markdown"
    require(bool(text.strip()), f"{role}: empty input")
    require(len(text) <= limits.max_chars, f"{role}: {len(text)} characters > {limits.max_chars}")
    require(len(text.splitlines()) <= limits.max_lines, f"{role}: exceeds line limit")
    return {"source_id": role, "role": role, "path": str(p.resolve()), "sha256": sha(raw),
            "text_sha256": sha(text), "selector": selector, "text": text}


def _units(text: str) -> tuple[list[str], list[int]]:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return lines, offsets


def _fragments(sources: dict, limits: AnalysisLimits) -> list[dict]:
    system, human = sources["system"]["text"], sources["human"]["text"]
    a, ao = _units(system)
    b, bo = _units(human)
    fragments, preservation = [], None
    for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            if preservation is None and ao[j] > ao[i]:
                length = min(limits.fragment_chars, ao[j] - ao[i])
                preservation = {"kind": "unchanged_sample", "system": [ao[i], ao[i] + length],
                                "human": [bo[k], bo[k] + length]}
            continue
        # Line-level hunks, not semantic/audio alignment. Never truncate changed text.
        require(max(ao[j] - ao[i], bo[l] - bo[k]) <= limits.fragment_chars,
                f"changed hunk exceeds {limits.fragment_chars} characters; provide smaller explicit input excerpts")
        def window(start, end, text):
            room = limits.fragment_chars - (end - start)
            left = min(80, room // 2, start)
            right = min(80, room - left, len(text) - end)
            return [start - left, end + right]
        fragments.append({"kind": "change", "system": window(ao[i], ao[j], system),
                          "human": window(bo[k], bo[l], human),
                          "changed_system": [ao[i], ao[j]], "changed_human": [bo[k], bo[l]]})
    if preservation:
        fragments.append(preservation)
    for index, fragment in enumerate(fragments, 1):
        fragment["fragment_id"] = f"f{index:04d}"
        human_span = fragment["human"]
        needle = human[human_span[0]:human_span[1]]
        asr = sources["asr"]["text"]
        hits = list(re.finditer(re.escape(needle), asr)) if needle else []
        fragment["asr_alignment"] = ({"method": "unique_literal", "span": [hits[0].start(), hits[0].end()]}
                                     if len(hits) == 1 else {"method": "not-established"})
    return fragments


def _reference_metadata(value: Any, scope: dict) -> None:
    fields(value, {"source_kind", "version", "book_id"}, {"pdf_sha256", "notes"})
    require(isinstance(value["source_kind"], str) and value["source_kind"] in {"reference_text", "ocr_text"}, "invalid reference source_kind")
    for key in ("version", "book_id"):
        string(value[key], key, 200)
    require(not scope.get("book_id") or value["book_id"] == scope["book_id"], "reference book_id conflict")
    if "pdf_sha256" in value:
        require(isinstance(value["pdf_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", value["pdf_sha256"]),
                "invalid PDF hash")
    if "notes" in value:
        string(value["notes"], "reference notes", 1000)


def prepare_analysis(asr_path, system_path, human_path, *, metadata: dict,
                     reference_path=None, reference_metadata=None,
                     limits: AnalysisLimits = AnalysisLimits(), selected_fragment_ids=None) -> dict:
    validate_metadata(metadata)
    for key, value in asdict(limits).items():
        integer(value, key, 1)
    paths = [Path(p) for p in (asr_path, system_path, human_path)]
    if reference_path is not None:
        paths.append(Path(reference_path))
    require(all(p.is_file() for p in paths), "all explicit source paths must be files")
    require(all(not a.samefile(b) for i, a in enumerate(paths) for b in paths[i + 1:]),
            "source paths/inodes must be distinct")
    sources = {role: _read_source(p, role, limits) for role, p in zip(("asr", "system", "human"), paths)}
    if reference_path is not None:
        _reference_metadata(reference_metadata, metadata["scope"])
        sources["reference"] = _read_source(reference_path, "reference", limits)
        sources["reference"]["reference_metadata"] = reference_metadata
    else:
        require(reference_metadata is None, "reference metadata without reference")
    all_fragments = _fragments(sources, limits)
    ids = [f["fragment_id"] for f in all_fragments]
    selected = ids if selected_fragment_ids is None else list(selected_fragment_ids)
    require(len(selected) == len(set(selected)) and set(selected) <= set(ids), "unknown/duplicate fragment IDs")
    require(len(selected) <= limits.max_fragments,
            f"{len(selected)} fragments > {limits.max_fragments}; explicitly select IDs from {ids}")
    omitted = [i for i in ids if i not in selected]
    request = {"schema_version": 1, "metadata": metadata, "sources": sources,
               "fragments": [f for f in all_fragments if f["fragment_id"] in selected],
               "coverage": {"total_changes": sum(f["kind"] == "change" for f in all_fragments),
                            "selected_ids": [i for i in ids if i in selected], "omitted_ids": omitted,
                            "partial": bool(omitted), "preservation_sampling": "first equal span only"},
               "limits": asdict(limits),
               "analysis_prompt": (PROMPTS / "proofreading_memory_analysis.md").read_text(encoding="utf-8")}
    request["prompt_sha256"] = sha(request["analysis_prompt"])
    request["request_id"] = sha(canonical(request))
    require(len(canonical(request).encode("utf-8")) <= 1024 * 1024, "self-contained request exceeds 1 MiB")
    _check_payload_budget(request)
    return request


def _network_data(request: dict) -> dict:
    sources = {}
    for role, source in request["sources"].items():
        ranges = ([fragment[role] for fragment in request["fragments"]]
                  if role in {"system", "human"} else [[0, len(source["text"])]])
        sources[role] = {k: v for k, v in source.items() if k not in {"path", "text"}}
        sources[role]["windows"] = [{"start": start, "end": end, "text": source["text"][start:end]}
                                   for start, end in ranges]
    return {"schema_version": 1, "request_id": request["request_id"], "metadata": request["metadata"],
            "sources": sources, "fragments": request["fragments"], "coverage": request["coverage"]}


def _check_payload_budget(request: dict) -> None:
    size = len(request["analysis_prompt"]) + len(canonical(_network_data(request)))
    require(size <= request["limits"]["payload_chars"],
            f"model instructions+data {size} characters > {request['limits']['payload_chars']}; explicitly select fewer fragments or smaller inputs")


def validate_request(request: Any) -> None:
    fields(request, {"schema_version", "request_id", "metadata", "sources", "fragments", "coverage",
                     "limits", "analysis_prompt", "prompt_sha256"})
    require(type(request["schema_version"]) is int and request["schema_version"] == 1, "unknown request schema")
    require(request["request_id"] == sha(canonical({k: v for k, v in request.items() if k != "request_id"})),
            "request fingerprint mismatch")
    validate_metadata(request["metadata"])
    fields(request["limits"], set(asdict(AnalysisLimits())))
    for key, value in request["limits"].items():
        integer(value, key, 1)
    limits = AnalysisLimits(**request["limits"])
    string(request["analysis_prompt"], "analysis prompt", limits.payload_chars)
    require(sha(request["analysis_prompt"]) == request["prompt_sha256"], "prompt hash mismatch")
    fields(request["sources"], {"asr", "system", "human"}, {"reference"})
    for role, source in request["sources"].items():
        fields(source, {"source_id", "role", "path", "sha256", "text_sha256", "selector", "text"},
               {"reference_metadata"} if role == "reference" else set())
        require(source["source_id"] == role and source["role"] == role, "source role mismatch")
        string(source["text"], "source text", limits.max_chars)
        require(len(source["text"].splitlines()) <= limits.max_lines, "source line limit exceeded")
        require(source["text_sha256"] == sha(source["text"]), "source text hash mismatch")
        require(isinstance(source["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", source["sha256"]), "invalid raw hash")
        require(source["selector"] == "text" or (role == "system" and source["selector"] == "final_markdown"),
                "invalid selector")
        # JSON raw bytes are not embedded; retain their raw hash separately from decoded body hash.
        if source["selector"] == "text":
            require(source["sha256"] == source["text_sha256"], "raw/text hash mismatch")
        string(source["path"], "source path", 4096)
        if role == "reference":
            _reference_metadata(source.get("reference_metadata"), request["metadata"]["scope"])
    all_fragments = _fragments(request["sources"], limits)
    ids = [f["fragment_id"] for f in all_fragments]
    fields(request["coverage"], {"total_changes", "selected_ids", "omitted_ids", "partial", "preservation_sampling"})
    coverage = request["coverage"]
    selected = coverage["selected_ids"]
    require(isinstance(selected, list) and all(isinstance(x, str) for x in selected), "invalid selected IDs")
    require(len(set(selected)) == len(selected) and set(selected) <= set(ids), "invalid selected IDs")
    require(selected == [i for i in ids if i in selected], "selected IDs must be in source order")
    require(len(selected) <= limits.max_fragments, "too many selected fragments")
    omitted = [i for i in ids if i not in selected]
    require(coverage == {"total_changes": sum(f["kind"] == "change" for f in all_fragments),
                         "selected_ids": selected, "omitted_ids": omitted, "partial": bool(omitted),
                         "preservation_sampling": "first equal span only"}, "coverage mismatch")
    require(request["fragments"] == [f for f in all_fragments if f["fragment_id"] in selected], "fragment mismatch")
    _check_payload_budget(request)


def pdf_pages(source: dict, start: int, end: int) -> list[int] | None:
    if source["role"] != "reference":
        return None
    marks, offset = [], 0
    for line in source["text"].splitlines(keepends=True):
        if is_ocr_page_break_line(line):
            nums = [int(n) for n in re.findall(r"\d+", line)]
            marks.append((offset, offset + len(line), nums[0], nums[1]))
        offset += len(line)
    if not marks or any(a[3] != b[2] for a, b in zip(marks, marks[1:])):
        return None
    # A citation of an engineering marker itself is not book evidence.
    require(not any(start < finish and end > begin for begin, finish, _, _ in marks),
            "reference evidence intersects engineering page marker; split citation")
    def page(position):
        return next((previous for begin, _, previous, _ in marks if position < begin), marks[-1][3])
    return [page(start), page(end - 1)]


def validate_proposals(request: dict, response: Any) -> list[dict]:
    validate_request(request)
    require(len(canonical(response).encode("utf-8")) <= 1024 * 1024, "response exceeds 1 MiB")
    fields(response, {"schema_version", "request_id", "candidates"})
    require(type(response["schema_version"]) is int and response["schema_version"] == 1, "unknown response schema")
    require(response["request_id"] == request["request_id"], "response request_id mismatch")
    require(isinstance(response["candidates"], list) and len(response["candidates"]) <= 50, "candidates must be list <= 50")
    fragments = {f["fragment_id"]: f for f in request["fragments"]}
    result = []
    for candidate in response["candidates"]:
        fields(candidate, {"kind", "scope", "text", "reason", "retrieval_keys", "fragment_ids", "evidence"},
               {"observed_form", "preferred_form"})
        require(isinstance(candidate["kind"], str) and candidate["kind"] in KINDS, "invalid candidate kind")
        validate_scope(candidate["scope"], complete=False)
        require(candidate["scope"] == request["metadata"]["scope"], "candidate scope conflict/expansion")
        string(candidate["text"], "candidate text", 800)
        string(candidate["reason"], "candidate reason", 300)
        for key in ("observed_form", "preferred_form"):
            if key in candidate:
                string(candidate[key], key, 80)
        keys = candidate["retrieval_keys"]
        require(isinstance(keys, list) and len(keys) <= 12, "retrieval_keys must be list <=12")
        for key in keys:
            string(key, "retrieval key", 80)
        fids = candidate["fragment_ids"]
        require(isinstance(fids, list) and bool(fids) and all(isinstance(x, str) for x in fids), "invalid fragment_ids")
        require(len(fids) == len(set(fids)) and set(fids) <= fragments.keys(), "unselected/unknown fragment")
        evidence = candidate["evidence"]
        require(isinstance(evidence, list) and 1 <= len(evidence) <= 12, "evidence requires 1..12 citations")
        roles, excerpts, enriched = set(), {}, []
        for citation in evidence:
            fields(citation, {"source_id", "start", "end", "excerpt", "pdf_page"})
            sid = citation["source_id"]
            require(isinstance(sid, str) and sid in request["sources"], "unknown evidence source")
            source = request["sources"][sid]
            start, end = citation["start"], citation["end"]
            integer(start, "evidence start")
            integer(end, "evidence end", 1)
            require(start < end <= len(source["text"]), "evidence offset out of range")
            string(citation["excerpt"], "evidence excerpt", 800)
            require(citation["excerpt"] == source["text"][start:end], "forged evidence excerpt")
            if sid in {"system", "human"}:
                require(any(fragments[fid][sid][0] <= start < end <= fragments[fid][sid][1] for fid in fids),
                        "citation outside selected fragment windows")
            pages = citation["pdf_page"]
            require(pages is None or (isinstance(pages, list) and len(pages) == 2
                    and all(type(page) is int and page >= 1 for page in pages)), "invalid PDF page range")
            require(pages == pdf_pages(source, start, end), "forged/unknown PDF page")
            roles.add(sid)
            excerpts.setdefault(sid, []).append(citation["excerpt"])
            enriched.append({**citation, "source_sha256": source["sha256"], "text_sha256": source["text_sha256"],
                             "selector": source["selector"], "role": source["role"],
                             "reference_metadata": source.get("reference_metadata"),
                             "page_note": "physical PDF marker range" if citation["pdf_page"] else "PDF page not established"})
        if candidate["kind"] != "term":
            require({"system", "human"} <= roles, "editing/preservation candidate requires system+human evidence")
        if candidate["kind"] == "preservation_case":
            require(any(x in excerpts["human"] for x in excerpts["system"]), "preservation requires identical evidence")
            require(any(fragments[fid]["kind"] == "unchanged_sample" for fid in fids), "preservation needs unchanged sample")
        warnings = []
        if roles == {"reference"}:
            warnings.append("reference_only: not a verified definition; human review required")
            if request["sources"]["reference"]["reference_metadata"]["source_kind"] == "ocr_text":
                warnings.append("ocr_only: OCR may be wrong; never automatically authoritative")
        result.append({**candidate, "evidence": enriched, "warnings": warnings,
                       "dataset_kind": request["metadata"]["dataset_kind"], "request_id": request["request_id"]})
    return result


def analyze_request(request: dict, *, allow_network: bool = False, loaded_settings=None,
                    model=None, client=None, max_output_tokens=None) -> dict:
    require(allow_network is True, "analysis requires explicit --allow-network; selected text will be sent to configured backend")
    validate_request(request)
    if loaded_settings is None:
        from src.config_loader import load_settings
        loaded_settings = load_settings()
    settings = loaded_settings.settings
    if client is None:
        from src.codex_lb_client import CodexLBClient
        client = CodexLBClient(settings.codex_lb, timeout_seconds=settings.llm.timeout_seconds)
    selected_model = model or settings.llm.model
    string(selected_model, "model", 200)
    tokens = max_output_tokens if max_output_tokens is not None else settings.llm.max_output_tokens
    integer(tokens, "max_output_tokens", 1)
    payload = {"model": selected_model, "instructions": request["analysis_prompt"],
               "input": canonical(_network_data(request)), "stream": True, "store": False,
               "max_output_tokens": tokens}
    if settings.llm.reasoning_effort:
        payload["reasoning"] = {"effort": settings.llm.reasoning_effort}
    try:
        text = client.responses_stream_text(payload)
    except Exception as exc:
        # Do not persist/log transport details that may contain headers or secrets.
        raise MemoryExperimentError("Responses analysis failed; no candidates stored") from exc
    response = parse_json(text)
    validate_proposals(request, response)
    return response


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _dedup(candidate: dict) -> str:
    return sha(canonical({"kind": candidate["kind"], "scope": candidate["scope"],
                          "dataset_kind": candidate["dataset_kind"], "text": _normal(candidate["text"]),
                          "observed_form": _normal(candidate.get("observed_form", "")),
                          "preferred_form": _normal(candidate.get("preferred_form", ""))}))


_OCCURRENCES_SCHEMA = """CREATE TABLE memory_occurrences(
    occurrence_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(memory_id),
    request_id TEXT NOT NULL REFERENCES analysis_requests(request_id),
    proposal_json TEXT NOT NULL,
    response_mode TEXT NOT NULL
)"""


def _append_occurrence(db: sqlite3.Connection, memory_id: str, candidate: dict) -> tuple[str, bool]:
    # Transport declarations are not source identity. Preserve the first declaration on replay.
    proposal = {key: value for key, value in candidate.items() if key != "response_mode"}
    payload = canonical(proposal)
    request_id = proposal["request_id"]
    occurrence_id = "occ-" + sha(canonical({"request_id": request_id, "proposal": proposal}))
    existing = db.execute("SELECT memory_id,request_id,proposal_json FROM memory_occurrences WHERE occurrence_id=?",
                          (occurrence_id,)).fetchone()
    if existing is not None:
        require(tuple(existing) == (memory_id, request_id, payload), "occurrence identity conflict")
        return occurrence_id, False
    db.execute("INSERT INTO memory_occurrences VALUES (?,?,?,?,?)",
               (occurrence_id, memory_id, request_id, payload, candidate["response_mode"]))
    return occurrence_id, True


class MemoryStore:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        require(p.suffix.lower() in {".sqlite3", ".sqlite", ".db"}, "database requires .sqlite3/.sqlite/.db path")
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 2}:
            self.db.close()
            message = ("memory database schema v1 requires explicit migrate-v1 before opening"
                       if version == 1 else "unknown memory database schema version")
            raise MemoryExperimentError(message)
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and tables:
            self.db.close()
            raise MemoryExperimentError("refusing unversioned existing database")
        if version == 0:
            with self.db:
                self.db.executescript("""
                CREATE TABLE analysis_requests(request_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, response_mode TEXT NOT NULL);
                CREATE TABLE memories(memory_id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE NOT NULL, current_version INTEGER NOT NULL, current_status TEXT NOT NULL);
                CREATE TABLE memory_keys(dedup_key TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(memory_id));
                CREATE TABLE memory_versions(memory_id TEXT NOT NULL REFERENCES memories(memory_id), version INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, review_json TEXT NOT NULL, PRIMARY KEY(memory_id,version));
                CREATE TABLE audit_events(event_id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(memory_id), action TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL, from_version INTEGER, to_version INTEGER NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                """)
                self.db.execute(_OCCURRENCES_SCHEMA)
                self.db.execute("PRAGMA user_version=2")

    @staticmethod
    def migrate_v1(db_path: str | Path) -> dict:
        """Explicit transactional v1 -> v2 migration; never edits existing versions or audit."""
        p = Path(db_path)
        require(p.is_file() and p.suffix.lower() in {".sqlite3", ".sqlite", ".db"},
                "migration requires an existing memory database file")
        db = sqlite3.connect(p)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            require(db.execute("PRAGMA user_version").fetchone()[0] == 1, "migrate-v1 requires schema v1")
            db.execute(_OCCURRENCES_SCHEMA)
            count = 0
            for memory_id, payload in db.execute("SELECT memory_id,payload FROM memory_versions ORDER BY memory_id,version").fetchall():
                candidate = parse_json(payload)
                mode = candidate.get("response_mode")
                require(mode in {"offline", "replay", "remote", "manual-revision"}, "invalid v1 proposal mode")
                request_row = db.execute("SELECT request_json FROM analysis_requests WHERE request_id=?",
                                         (candidate.get("request_id"),)).fetchone()
                require(request_row is not None, "v1 proposal request missing")
                request = parse_json(request_row[0])
                # Revalidate the original proposal and derived citations before backfilling.
                raw = {key: candidate[key] for key in (
                    "kind", "scope", "text", "reason", "retrieval_keys", "fragment_ids", "observed_form", "preferred_form"
                ) if key in candidate}
                raw["evidence"] = [{key: citation[key] for key in ("source_id", "start", "end", "excerpt", "pdf_page")}
                                   for citation in candidate["evidence"]]
                validated = validate_proposals(request, {"schema_version": 1, "request_id": request["request_id"],
                                                        "candidates": [raw]})[0]
                require(canonical(validated) == canonical({key: value for key, value in candidate.items()
                                                          if key != "response_mode"}), "v1 proposal payload mismatch")
                _, inserted = _append_occurrence(db, memory_id, candidate)
                count += inserted
            db.execute("PRAGMA user_version=2")
            db.commit()
            return {"from_schema": 1, "to_schema": 2, "occurrences_backfilled": count}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def import_candidates(self, request: dict, response: dict, *, response_mode: str) -> dict:
        require(response_mode in {"offline", "replay", "remote"}, "explicit response_mode required")
        candidates = validate_proposals(request, response)
        inserted, duplicates, occurrence_inserted, occurrence_duplicates = [], [], [], []
        with self.db:
            existing = self.db.execute("SELECT request_json FROM analysis_requests WHERE request_id=?", (request["request_id"],)).fetchone()
            require(existing is None or existing[0] == canonical(request), "request ID conflict")
            self.db.execute("INSERT OR IGNORE INTO analysis_requests VALUES (?,?,?)",
                            (request["request_id"], canonical(request), response_mode))
            for candidate in candidates:
                candidate = {**candidate, "response_mode": response_mode}
                key = _dedup(candidate)
                row = self.db.execute("SELECT memory_id FROM memory_keys WHERE dedup_key=?", (key,)).fetchone()
                if row:
                    mid = row[0]
                    duplicates.append(mid)
                else:
                    mid = "mem-" + key
                    self.db.execute("INSERT INTO memories VALUES (?,?,1,'pending')", (mid, key))
                    self.db.execute("INSERT INTO memory_keys VALUES (?,?)", (key, mid))
                    self.db.execute("INSERT INTO memory_versions VALUES (?,1,?,'pending','{}')", (mid, canonical(candidate)))
                    self.db.execute("INSERT INTO audit_events(memory_id,action,reviewer,reason,from_version,to_version) VALUES (?,'import','proposal',?,NULL,1)", (mid, response_mode))
                    inserted.append(mid)
                oid, added = _append_occurrence(self.db, mid, candidate)
                (occurrence_inserted if added else occurrence_duplicates).append(oid)
        return {"inserted": inserted, "duplicates": duplicates, "candidate_count": len(candidates),
                "occurrence_inserted": occurrence_inserted, "occurrence_duplicates": occurrence_duplicates}

    def _version_entries(self, *, status: str | None = None) -> list[dict]:
        require(status is None or status in {"pending", "approved", "rejected", "disabled"}, "invalid status")
        rows = self.db.execute("""SELECT m.memory_id,m.current_version,m.current_status,v.payload,v.review_json
            FROM memories m JOIN memory_versions v ON v.memory_id=m.memory_id AND v.version=m.current_version
            WHERE (? IS NULL OR m.current_status=?) ORDER BY m.memory_id""", (status, status)).fetchall()
        return [{"memory_id": r[0], "version": r[1], "status": r[2], "candidate": parse_json(r[3]),
                 "review": parse_json(r[4])} for r in rows]

    def list_entries(self, *, status: str | None = None) -> list[dict]:
        entries = self._version_entries(status=status)
        for entry in entries:
            rows = self.db.execute("""SELECT occurrence_id,request_id,proposal_json,response_mode
                FROM memory_occurrences WHERE memory_id=? ORDER BY occurrence_id""", (entry["memory_id"],)).fetchall()
            entry["occurrences"] = [{"occurrence_id": row[0], "request_id": row[1], "proposal": parse_json(row[2]),
                                     "response_mode": row[3], "review_status": "unreviewed"} for row in rows]
        return entries

    def audit_events(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM audit_events ORDER BY event_id")]

    def _current(self, mid: str, expected_version: int):
        integer(expected_version, "expected_version", 1)
        row = self.db.execute("""SELECT m.current_version,m.current_status,v.payload,v.review_json
            FROM memories m JOIN memory_versions v ON v.memory_id=m.memory_id AND v.version=m.current_version
            WHERE m.memory_id=?""", (mid,)).fetchone()
        require(row is not None, "unknown memory ID")
        require(row[0] == expected_version, "stale expected_version")
        return row

    def review(self, memory_id: str, *, action: str, reviewer: str, reason: str, expected_version: int) -> dict:
        require(action in {"approve", "reject", "disable"}, "invalid review action")
        string(reviewer, "reviewer", 200)
        string(reason, "review reason", 1000)
        target = {"approve": "approved", "reject": "rejected", "disable": "disabled"}[action]
        with self.db:
            # Obtain the write lock before reading to enforce optimistic version checks across processes.
            self.db.execute("UPDATE memories SET current_version=current_version WHERE memory_id=?", (memory_id,))
            row = self._current(memory_id, expected_version)
            if row[1] == target:
                return {"memory_id": memory_id, "version": row[0], "status": target, "changed": False}
            require((row[1], target) in {("pending", "approved"), ("pending", "rejected"), ("approved", "disabled")}, "illegal status transition")
            if action == "approve":
                validate_scope(parse_json(row[2])["scope"], complete=True)
            version = row[0] + 1
            review = {"reviewer": reviewer, "reason": reason, "action": action}
            self.db.execute("INSERT INTO memory_versions VALUES (?,?,?,?,?)",
                            (memory_id, version, row[2], target, canonical(review)))
            self.db.execute("UPDATE memories SET current_version=?,current_status=? WHERE memory_id=?", (version, target, memory_id))
            self.db.execute("INSERT INTO audit_events(memory_id,action,reviewer,reason,from_version,to_version) VALUES (?,?,?,?,?,?)", (memory_id, action, reviewer, reason, row[0], version))
        return {"memory_id": memory_id, "version": version, "status": target, "changed": True}

    def revise(self, memory_id: str, *, replacement: dict, reviewer: str, reason: str, expected_version: int) -> dict:
        string(reviewer, "reviewer", 200)
        string(reason, "revision reason", 1000)
        with self.db:
            self.db.execute("UPDATE memories SET current_version=current_version WHERE memory_id=?", (memory_id,))
            row = self._current(memory_id, expected_version)
            old = parse_json(row[2])
            request = parse_json(self.db.execute("SELECT request_json FROM analysis_requests WHERE request_id=?", (old["request_id"],)).fetchone()[0])
            candidate = validate_proposals(request, {"schema_version": 1, "request_id": request["request_id"], "candidates": [replacement]})[0]
            require(candidate["kind"] == old["kind"] and candidate["scope"] == old["scope"], "revision cannot change kind/scope")
            candidate["response_mode"] = "manual-revision"
            key = _dedup(candidate)
            collision = self.db.execute("SELECT memory_id FROM memory_keys WHERE dedup_key=?", (key,)).fetchone()
            require(collision is None or collision[0] == memory_id, "revision collides with existing candidate")
            version = row[0] + 1
            self.db.execute("INSERT INTO memory_versions VALUES (?,?,?,'pending',?)", (memory_id, version, canonical(candidate), canonical({"reviewer": reviewer, "reason": reason, "action": "revise"})))
            self.db.execute("UPDATE memories SET current_version=?,current_status='pending',dedup_key=? WHERE memory_id=?", (version, key, memory_id))
            self.db.execute("INSERT OR IGNORE INTO memory_keys VALUES (?,?)", (key, memory_id))
            _append_occurrence(self.db, memory_id, candidate)
            self.db.execute("INSERT INTO audit_events(memory_id,action,reviewer,reason,from_version,to_version) VALUES (?,'revise',?,?,?,?)", (memory_id, reviewer, reason, row[0], version))
        return {"memory_id": memory_id, "version": version, "status": "pending", "changed": True}

    def export_context(self, *, task_scope: dict, dataset_kind: str, query: str,
                       max_items: int = 8, max_chars: int = 4000) -> dict:
        complete = validate_scope(task_scope, complete=False)
        require(isinstance(dataset_kind, str) and dataset_kind in DATASETS, "invalid task dataset_kind")
        require(isinstance(query, str) and len(query) <= 100_000, "query must be text <=100000 chars")
        integer(max_items, "max_items")
        integer(max_chars, "max_chars")
        context = {"schema_version": 1, "task_scope": task_scope, "dataset_kind": dataset_kind,
                   "query": query, "query_sha256": sha(query), "budgets": {"max_items": max_items, "max_chars": max_chars},
                   "selection_policy_version": 1,
                   "block_instructions": (PROMPTS / "proofreading_memory_context.md").read_text(encoding="utf-8"),
                   "selected": [], "omitted": [], "notes": [], "eligible_count": 0}
        if not complete:
            context["notes"].append("missing_scope: no cross-book/speaker fallback")
        ranked = []
        normal_query = _normal(query)
        if complete:
            # Inspection includes unreviewed occurrences; model consumption must not.
            for entry in self._version_entries():
                c = entry["candidate"]
                if c["scope"] != task_scope or c["dataset_kind"] != dataset_kind:
                    continue
                if entry["status"] != "approved":
                    context["omitted"].append({"memory_id": entry["memory_id"], "version": entry["version"], "reason": "not-approved:" + entry["status"]})
                    continue
                context["eligible_count"] += 1
                keys = list(dict.fromkeys(c["retrieval_keys"] + [c[k] for k in ("observed_form", "preferred_form") if k in c]))
                matched = [key for key in keys if _normal(key) in normal_query]
                if matched:
                    score, method = 100 + max(len(_normal(key)) for key in matched), "literal_normalized"
                elif c["kind"] == "reading_preference":
                    score, method = 1, "task_scope_preference"
                else:
                    context["omitted"].append({"memory_id": entry["memory_id"], "version": entry["version"], "reason": "no-literal-match"})
                    continue
                ranked.append({**entry, "retrieval": {"score": score, "method": method, "matched_keys": matched}})
        ranked.sort(key=lambda e: (-e["retrieval"]["score"], e["candidate"]["kind"], e["memory_id"], e["version"]))
        for entry in ranked:
            reason = None
            if len(context["selected"]) >= max_items:
                reason = "item-budget"
            else:
                proposed = context["selected"] + [entry]
                if len(_block(context, proposed)) > max_chars:
                    reason = "character-budget"
            if reason:
                context["omitted"].append({"memory_id": entry["memory_id"], "version": entry["version"], "reason": reason})
            else:
                context["selected"].append(entry)
        context["omitted"].sort(key=lambda e: (e["memory_id"], e["version"], e["reason"]))
        context["block_chars"] = len(_block(context, context["selected"]))
        context["fingerprint"] = sha(canonical(context))
        return context


def _block(context: dict, selected: list[dict]) -> str:
    # An empty selection produces no model data, including for a legitimate zero budget.
    if not selected:
        return ""
    data = {"schema_version": 1, "task_scope": context["task_scope"], "dataset_kind": context["dataset_kind"],
            "memories": selected}
    return context["block_instructions"] + "\n<proofreading-memory-data>\n" + canonical(data) + "\n</proofreading-memory-data>\n"


def render_context_block(context: dict) -> str:
    fields(context, {"schema_version", "task_scope", "dataset_kind", "query", "query_sha256", "budgets",
                     "selection_policy_version", "block_instructions", "selected", "omitted", "notes",
                     "eligible_count", "block_chars", "fingerprint"})
    require(context["fingerprint"] == sha(canonical({k: v for k, v in context.items() if k != "fingerprint"})), "context fingerprint mismatch")
    require(type(context["schema_version"]) is int and context["schema_version"] == 1
            and type(context["selection_policy_version"]) is int and context["selection_policy_version"] == 1,
            "unknown context schema/policy")
    validate_scope(context["task_scope"], complete=bool(context["selected"]))
    require(isinstance(context["dataset_kind"], str) and context["dataset_kind"] in DATASETS, "invalid frozen dataset")
    require(isinstance(context["query"], str) and len(context["query"]) <= 100_000, "invalid frozen query")
    require(context["query_sha256"] == sha(context["query"]), "query hash mismatch")
    string(context["block_instructions"], "frozen instructions", 40000)
    integer(context["eligible_count"], "eligible_count")
    integer(context["block_chars"], "block_chars")
    require(isinstance(context["notes"], list) and all(isinstance(note, str) for note in context["notes"]), "invalid frozen notes")
    require(isinstance(context["omitted"], list), "invalid frozen omissions")
    for omission in context["omitted"]:
        fields(omission, {"memory_id", "version", "reason"})
        string(omission["memory_id"], "omitted ID", 100)
        integer(omission["version"], "omitted version", 1)
        string(omission["reason"], "omission reason", 200)
    fields(context["budgets"], {"max_items", "max_chars"})
    for key, value in context["budgets"].items():
        integer(value, key)
    require(isinstance(context["selected"], list), "invalid frozen selection")
    for entry in context["selected"]:
        fields(entry, {"memory_id", "version", "status", "candidate", "review", "retrieval"})
        string(entry["memory_id"], "frozen memory ID", 100)
        integer(entry["version"], "frozen memory version", 1)
        c = entry["candidate"]
        fields(c, {"kind", "scope", "text", "reason", "retrieval_keys", "fragment_ids", "evidence",
                   "warnings", "dataset_kind", "request_id", "response_mode"}, {"observed_form", "preferred_form"})
        require(isinstance(c["kind"], str) and c["kind"] in KINDS, "invalid frozen kind")
        string(c["text"], "frozen memory text", 800)
        string(c["reason"], "frozen memory reason", 300)
        require(entry["status"] == "approved" and c["scope"] == context["task_scope"]
                and c["dataset_kind"] == context["dataset_kind"], "invalid frozen scope/status")
        require(isinstance(c["evidence"], list) and 1 <= len(c["evidence"]) <= 12, "invalid frozen evidence")
        for citation in c["evidence"]:
            fields(citation, {"source_id", "start", "end", "excerpt", "pdf_page", "source_sha256", "text_sha256",
                              "selector", "role", "reference_metadata", "page_note"})
            integer(citation["start"], "frozen evidence start")
            integer(citation["end"], "frozen evidence end", 1)
            string(citation["excerpt"], "frozen evidence excerpt", 800)
            require(len(citation["excerpt"]) == citation["end"] - citation["start"], "invalid frozen citation length")
            for key in ("source_sha256", "text_sha256"):
                require(isinstance(citation[key], str) and re.fullmatch(r"[a-f0-9]{64}", citation[key]), "invalid frozen source hash")
        fields(entry["review"], {"action", "reviewer", "reason"})
        require(entry["review"]["action"] == "approve", "missing frozen approval provenance")
        string(entry["review"]["reviewer"], "frozen reviewer", 200)
        string(entry["review"]["reason"], "frozen review reason", 1000)
        fields(entry["retrieval"], {"score", "method", "matched_keys"})
        integer(entry["retrieval"]["score"], "retrieval score", 1)
    block = _block(context, context["selected"])
    require(len(block) == context["block_chars"] and len(block) <= context["budgets"]["max_chars"]
            and len(context["selected"]) <= context["budgets"]["max_items"], "invalid frozen budget")
    return block


def write_artifact(path: str | Path, content: str, *, protected_paths=()) -> None:
    p = Path(path)
    require(not p.exists() and not p.is_symlink(), "refusing existing output (including inputs)")
    require(all(p.resolve() != Path(other).resolve() for other in protected_paths), "output would overwrite input")
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, delete=False) as stream:
            temp = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic, no-clobber publication. Never replace an old frozen file.
        os.link(temp, p)
    except OSError as exc:
        raise MemoryExperimentError("cannot publish artifact without overwriting") from exc
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)


def export_analysis_request(request: dict, output_path: str | Path) -> None:
    validate_request(request)
    write_artifact(output_path, canonical(request) + "\n", protected_paths=[s["path"] for s in request["sources"].values()])


def write_frozen_context(context: dict, output_path: str | Path, *, block_path=None) -> None:
    block = render_context_block(context)
    outputs = [Path(output_path)] + ([Path(block_path)] if block_path is not None else [])
    require(len({p.resolve() for p in outputs}) == len(outputs), "context and block outputs must differ")
    require(all(not p.exists() and not p.is_symlink() for p in outputs), "refusing existing frozen output")
    write_artifact(output_path, canonical(context) + "\n")
    if block_path is not None:
        write_artifact(block_path, block)
