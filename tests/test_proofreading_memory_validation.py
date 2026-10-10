"""Additional malformed-boundary checks for the independent memory module."""
from __future__ import annotations

import copy

import pytest

from src.proofreading_memory import (
    MemoryExperimentError, MemoryStore, canonical, pdf_pages,
    prepare_analysis, render_context_block, sha, validate_proposals,
)
from tests.test_proofreading_memory import (
    META, approve, candidate, context, package, response,
)


@pytest.mark.parametrize("bad", ["kind-list", "scope-list", "citation-list", "keys-string", "fids-list", "float-page", "bool-page", "bool-schema", "unknown-field"])
def test_untrusted_model_types_are_explicit_errors(package, bad):
    _, request = package
    c = candidate(request, "term")
    reply = response(request, c)
    if bad == "kind-list": c["kind"] = ["term"]
    elif bad == "scope-list": c["scope"] = []
    elif bad == "citation-list": c["evidence"][0] = []
    elif bad == "keys-string": c["retrieval_keys"] = "bad"
    elif bad == "fids-list": c["fragment_ids"] = [["f0001"]]
    elif bad == "float-page": c["evidence"][0]["pdf_page"] = [3.0, 3.0]
    elif bad == "bool-page": c["evidence"][0]["pdf_page"] = [True, True]
    elif bad == "bool-schema": reply["schema_version"] = True
    else: reply["approve"] = True
    with pytest.raises(MemoryExperimentError):
        validate_proposals(request, reply)


@pytest.mark.parametrize("bad", ["wrong-book", "missing-version", "dataset-list", "same-reference", "metadata-without-reference"])
def test_metadata_and_reference_refusal(package, bad):
    paths, request = package
    meta = copy.deepcopy(META)
    refmeta = copy.deepcopy(request["sources"]["reference"]["reference_metadata"])
    ref = paths["reference"]
    if bad == "wrong-book": refmeta["book_id"] = "different"
    elif bad == "missing-version": refmeta.pop("version")
    elif bad == "dataset-list": meta["dataset_kind"] = ["synthetic"]
    elif bad == "same-reference": ref = paths["asr"]
    else: ref = None
    with pytest.raises(MemoryExperimentError):
        prepare_analysis(paths["asr"], paths["system"], paths["human"], metadata=meta,
                         reference_path=ref, reference_metadata=refmeta)


def test_revision_from_approved_removes_old_and_snapshot_stays_frozen(package, tmp_path, monkeypatch):
    import src.proofreading_memory as module
    _, request = package
    with MemoryStore(tmp_path / "revision.db") as store:
        c = candidate(request)
        mid = store.import_candidates(request, response(request, c), response_mode="replay")["inserted"][0]
        approve(store, mid)
        frozen = context(store)
        rendered = render_context_block(frozen)
        c["text"] += "再次核验。"
        store.revise(mid, replacement=c, reviewer="synthetic", reason="revision", expected_version=2)
        assert not context(store)["selected"]
        assert render_context_block(frozen) == rendered
        # Old render never consults current prompt files.
        monkeypatch.setattr(module, "PROMPTS", tmp_path / "nonexistent-prompts")
        assert render_context_block(frozen) == rendered
        assert store.list_entries()[0]["candidate"]["response_mode"] == "manual-revision"
        with pytest.raises(MemoryExperimentError): approve(store, mid, 2)


def test_single_page_and_second_page_are_physical_markers(package):
    _, request = package
    source = request["sources"]["reference"]
    start = source["text"].index("专名")
    assert pdf_pages(source, start, start + 2) == [4, 4]


def test_response_byte_budget_and_invalid_database_path(package, tmp_path):
    _, request = package
    huge = response(request)
    huge["candidates"][0]["text"] = "字" * 400000
    with pytest.raises(MemoryExperimentError, match="1 MiB"):
        validate_proposals(request, huge)
    protected = tmp_path / "original.txt"
    protected.write_text("protected")
    with pytest.raises(MemoryExperimentError): MemoryStore(protected)
    assert protected.read_text() == "protected"


def test_rehashed_malformed_frozen_schema_is_rejected(package, tmp_path):
    _, request = package
    with MemoryStore(tmp_path / 'schema.db') as store:
        mid = store.import_candidates(request, response(request), response_mode='replay')['inserted'][0]
        approve(store, mid)
        frozen = context(store)
    frozen['schema_version'] = True
    frozen['fingerprint'] = sha(canonical({k: v for k, v in frozen.items() if k != 'fingerprint'}))
    with pytest.raises(MemoryExperimentError, match='schema/policy'):
        render_context_block(frozen)


def second_request(package):
    paths, first = package
    paths['human'].write_text(paths['human'].read_text(encoding='utf-8') + '第二次构造人工修改，非独立质量确认。\n', encoding='utf-8')
    request = prepare_analysis(paths['asr'], paths['system'], paths['human'], metadata=copy.deepcopy(META))
    assert request['request_id'] != first['request_id']
    assert request['sources']['human']['sha256'] != first['sources']['human']['sha256']
    proposal = candidate(request)
    proposal['reason'] = '第二请求的独有理由与出处，仍未审核。'
    return request, proposal


def test_duplicate_experience_retains_unreviewed_occurrences_without_context_drift(package, tmp_path):
    _, first = package
    c = candidate(first)
    db = tmp_path / 'occurrences.db'
    with MemoryStore(db) as store:
        initial = store.import_candidates(first, response(first, c), response_mode='replay')
        mid = initial['inserted'][0]
        assert len(initial['occurrence_inserted']) == 1
        approve(store, mid)
        frozen = context(store)
        before_bytes, before_block = canonical(frozen), render_context_block(frozen)
        before_versions = [tuple(row) for row in store.db.execute('SELECT * FROM memory_versions ORDER BY version')]
        before_audit = store.audit_events()
        second, other = second_request(package)
        imported = store.import_candidates(second, response(second, other), response_mode='offline')
        assert imported['inserted'] == [] and imported['duplicates'] == [mid]
        assert len(imported['occurrence_inserted']) == 1
        assert imported['occurrence_duplicates'] == []
        entries = store.list_entries()
        assert len(entries) == 1 and entries[0]['version'] == 2 and entries[0]['status'] == 'approved'
        assert entries[0]['candidate']['request_id'] == first['request_id']
        occurrences = entries[0]['occurrences']
        assert len(occurrences) == 2
        assert {o['request_id'] for o in occurrences} == {first['request_id'], second['request_id']}
        added = next(o for o in occurrences if o['request_id'] == second['request_id'])
        assert added['proposal']['reason'] == other['reason']
        assert added['proposal']['evidence'][1]['source_sha256'] == second['sources']['human']['sha256']
        assert added['review_status'] == 'unreviewed' and added['response_mode'] == 'offline'
        assert 'response_mode' not in added['proposal']
        assert store.audit_events() == before_audit
        assert [tuple(row) for row in store.db.execute('SELECT * FROM memory_versions ORDER BY version')] == before_versions
        after = context(store)
        assert canonical(after) == before_bytes and render_context_block(after) == before_block
        assert 'occurrences' not in after['selected'][0]
        assert other['reason'] not in before_block
        replayed = store.import_candidates(second, response(second, other), response_mode='remote')
        assert replayed['occurrence_inserted'] == []
        assert replayed['occurrence_duplicates'] == imported['occurrence_inserted']
        assert store.list_entries()[0]['occurrences'] == occurrences  # First mode declaration wins.
        assert canonical(context(store)) == before_bytes
    with MemoryStore(db) as store:
        assert len(store.list_entries()[0]['occurrences']) == 2
        assert canonical(context(store)) == before_bytes


@pytest.mark.parametrize('status', ['rejected', 'disabled'])
def test_new_occurrence_does_not_revive_review_state(package, tmp_path, status):
    _, first = package
    with MemoryStore(tmp_path / 'closed.db') as store:
        mid = store.import_candidates(first, response(first), response_mode='replay')['inserted'][0]
        if status == 'disabled':
            approve(store, mid)
            store.review(mid, action='disable', reviewer='synthetic', reason='test', expected_version=2)
        else:
            store.review(mid, action='reject', reviewer='synthetic', reason='test', expected_version=1)
        before = store._version_entries()
        before_audit = store.audit_events()
        second, proposal = second_request(package)
        store.import_candidates(second, response(second, proposal), response_mode='offline')
        assert store._version_entries() == before and store.audit_events() == before_audit
        assert len(store.list_entries()[0]['occurrences']) == 2
        assert not context(store)['selected']


def test_occurrence_identity_same_request_different_proposal_and_revision(package, tmp_path):
    _, request = package
    c = candidate(request)
    with MemoryStore(tmp_path / 'identity.db') as store:
        first = store.import_candidates(request, response(request, c), response_mode='replay')
        mid = first['inserted'][0]
        c2 = copy.deepcopy(c)
        c2['reason'] = '同请求不同提案理由，必须保留。'
        second = store.import_candidates(request, response(request, c2), response_mode='offline')
        assert second['duplicates'] == [mid] and second['occurrence_inserted'] != first['occurrence_inserted']
        changed = copy.deepcopy(c)
        changed['text'] += '待审核的修订。'
        store.revise(mid, replacement=changed, reviewer='synthetic', reason='test', expected_version=1)
        before = store.list_entries()
        original = store.import_candidates(request, response(request, c), response_mode='remote')
        assert original['inserted'] == [] and original['occurrence_inserted'] == []
        assert original['occurrence_duplicates'] == first['occurrence_inserted']
        assert store.list_entries() == before and len(before[0]['occurrences']) == 3


def test_invalid_batch_has_zero_occurrence_or_request_pollution(package, tmp_path):
    _, request = package
    with MemoryStore(tmp_path / 'invalid.db') as store:
        good = candidate(request)
        bad = copy.deepcopy(good)
        bad['evidence'][0]['excerpt'] = '伪造'
        with pytest.raises(MemoryExperimentError):
            store.import_candidates(request, response(request, good, bad), response_mode='replay')
        for table in ('analysis_requests', 'memories', 'memory_versions', 'memory_keys', 'audit_events', 'memory_occurrences'):
            assert store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def make_v1_database(db_path, request):
    """The old v1 schema is exactly v2 minus memory_occurrences, with user_version=1."""
    with MemoryStore(db_path) as store:
        mid = store.import_candidates(request, response(request), response_mode='replay')['inserted'][0]
        approve(store, mid)
        frozen = context(store)
        replacement = candidate(request)
        replacement['text'] += '旧库历史修订。'
        store.revise(mid, replacement=replacement, reviewer='synthetic', reason='v1 revision', expected_version=2)
        approve(store, mid, 3)
        current = context(store)
        store.db.execute('DROP TABLE memory_occurrences')
        store.db.execute('PRAGMA user_version=1')
        store.db.commit()
        before = {table: [tuple(row) for row in store.db.execute(f'SELECT * FROM {table}')]
                  for table in ('analysis_requests', 'memories', 'memory_versions', 'memory_keys', 'audit_events')}
    return before, frozen, current


def test_explicit_v1_migration_preserves_versions_audit_and_frozen_context(package, tmp_path):
    import sqlite3
    _, request = package
    path = tmp_path / 'v1.db'
    before, frozen, current = make_v1_database(path, request)
    old_block = render_context_block(frozen)
    with pytest.raises(MemoryExperimentError, match='explicit migrate-v1'):
        MemoryStore(path)
    migrated = MemoryStore.migrate_v1(path)
    assert migrated == {'from_schema': 1, 'to_schema': 2, 'occurrences_backfilled': 2}
    with MemoryStore(path) as store:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == 2
        for table, rows in before.items():
            assert [tuple(row) for row in store.db.execute(f'SELECT * FROM {table}')] == rows
        assert canonical(context(store)) == canonical(current)
        assert render_context_block(frozen) == old_block
        entries = store.list_entries()
        assert len(entries) == 1 and len(entries[0]['occurrences']) == 2
        duplicate = store.import_candidates(request, response(request), response_mode='remote')
        assert duplicate['inserted'] == [] and duplicate['occurrence_inserted'] == []
        assert len(store.list_entries()[0]['occurrences']) == 2
    with pytest.raises(MemoryExperimentError, match='requires schema v1'):
        MemoryStore.migrate_v1(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


def test_invalid_v1_migration_rolls_back_schema_and_preserves_rows(package, tmp_path):
    import sqlite3
    _, request = package
    path = tmp_path / 'corrupt-v1.db'
    make_v1_database(path, request)
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT memory_id,payload FROM memory_versions WHERE version=1').fetchone()
        from src.proofreading_memory import parse_json
        invalid = parse_json(row[1])
        invalid['evidence'][0]['excerpt'] = '伪造'
        db.execute('UPDATE memory_versions SET payload=? WHERE memory_id=? AND version=1', (canonical(invalid), row[0]))
    with pytest.raises(MemoryExperimentError):
        MemoryStore.migrate_v1(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='memory_occurrences'").fetchall()
        assert db.execute('SELECT payload FROM memory_versions WHERE version=1').fetchone()[0] == canonical(invalid)


def test_cli_migrate_v1_and_list_exposes_occurrences_only_for_inspection(package, tmp_path):
    import json
    import subprocess
    from tests.test_proofreading_memory import ROOT
    _, request = package
    db = tmp_path / 'cli-v1.db'
    _, _, current = make_v1_database(db, request)
    cli = [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/12_memory_experiment.py')]
    refusal = subprocess.run(cli + ['list', '--db', str(db)], capture_output=True, text=True)
    assert refusal.returncode == 1 and 'migrate-v1' in refusal.stderr
    result = subprocess.run(cli + ['migrate-v1', '--db', str(db)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['occurrences_backfilled'] == 2
    listing = subprocess.run(cli + ['list', '--db', str(db)], capture_output=True, text=True)
    assert listing.returncode == 0 and len(json.loads(listing.stdout)[0]['occurrences']) == 2
    with MemoryStore(db) as store:
        assert canonical(context(store)) == canonical(current)


def test_occurrence_write_failure_rolls_back_whole_import(package, tmp_path):
    import sqlite3
    _, request = package
    with MemoryStore(tmp_path / 'write-failure.db') as store:
        store.db.execute("""CREATE TRIGGER refuse_occurrence BEFORE INSERT ON memory_occurrences
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match='simulated write failure'):
            store.import_candidates(request, response(request), response_mode='replay')
        for table in ('analysis_requests', 'memories', 'memory_versions', 'memory_keys', 'audit_events', 'memory_occurrences'):
            assert store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
