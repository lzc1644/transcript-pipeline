"""固定书库材料穿过真实参考准备阶段；模型与ASR仍由离线替身隔离。"""
import json
import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter

from src.align_utils import build_reference_blocks
from src.config_loader import load_settings
from src.library_reference import library_reference_fragments, require_clean_library_output
from src.web.state_store import read_json_file
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json


def prepared_library(app, root):
    books = []
    for index in (1, 2):
        path = root / f"book-{index}.pdf"
        writer = PdfWriter()
        for page in range(3):
            writer.add_blank_page(width=200 + index, height=300 + page)
        writer.write(path)
        book = app.state.library.add_book(path, title=f"书{index}", edition=f"版本{index}")
        for page, text in enumerate((f"书{index}第一页正文。", f"书{index}第二页正文。", ""), 1):
            app.state.library.save_page(book["id"], page, text=text, base_revision_id=None)
        books.append(book)
    material = app.state.library.create_material("第十二次读书会", [
        {"book_id": books[0]["id"], "start_page": 2, "end_page": 3},
        {"book_id": books[1]["id"], "start_page": 1, "end_page": 1},
        {"book_id": books[0]["id"], "start_page": 1, "end_page": 1},
    ])
    revision = app.state.library.publish_material(material["id"], base_version=material["version"])
    return books, material, revision


def test_library_submit_snapshot_txt_zero_ocr_and_independent_rerun(tmp_path, monkeypatch):
    from api_server import create_app
    from src.pipeline import run_stage as actual_run_stage
    from src.web.tasks import execute_single_job, run_job_rerun

    write_minimal_settings(tmp_path, reference_overrides={"enabled": False, "allow_txt": False})
    app = create_app(project_root=tmp_path, run_tasks_inline=True)
    books, material, revision = prepared_library(app, tmp_path)
    video = tmp_path / "meeting.wav"
    video.write_bytes(b"offline media fixture")
    prepared = []

    def forbidden_ocr(*args, **kwargs):
        pytest.fail("已准备的书库材料不能进入PDF OCR")

    monkeypatch.setattr("src.reference_utils.read_pdf_reference", forbidden_ocr)

    def stage(name, loaded, logger, **kwargs):
        if name == "prepare-reference":
            assert loaded.settings.reference.enabled and loaded.settings.reference.allow_txt
            assert [p.name for p in loaded.path_for("reference_dir").iterdir()] == ["source.txt"]
            result = actual_run_stage(name, loaded, logger, **kwargs)
            prepared.append((loaded.path_for("extracted_text_dir") / "source.txt").read_text())
            return result
        if name == "export-markdown":
            output = loaded.path_for("final_dir")
            (output / "source.md").write_text("# 已整理\n\n正文。", encoding="utf-8")
            (output / "source.txt").write_text("已整理\n\n正文。", encoding="utf-8")
        return 0

    monkeypatch.setattr("src.web.tasks.run_stage", stage)

    def execute(**kwargs):
        job_root = tmp_path / "data/jobs" / kwargs["job_id"]
        assert (job_root / "attachments/library/reference.pdf").is_file()
        assert (job_root / "attachments/library/sources.json").is_file()
        # 排队期间的页修改和全局提示词修改不改变本次材料和专属提示。
        old = app.state.library.get_page(books[0]["id"], 2)
        app.state.library.save_page(books[0]["id"], 2, text="后来修改的文字",
                                    base_revision_id=old["current_revision_id"])
        (tmp_path / "config/prompts/final_cleanup.md").write_text("未来提示词", encoding="utf-8")
        execute_single_job(**kwargs)

    app.state.execute_single_job = execute
    response = request_json(app, "POST", "/api/jobs", json_body={
        "video": str(video), "output_dir": str(tmp_path / "delivered"),
        "library_material_revision_id": revision["id"],
    })
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job_root = tmp_path / "data/jobs" / job_id
    state = read_json_file(job_root / "state.json")
    assert state["status"] == "success", state
    assert "书库：第十二次读书会" in state["input_summary"]["reference_source"]
    assert "书1第二页正文。" in prepared[0] and "后来修改" not in prepared[0]
    generated = load_settings(settings_path=job_root / "settings.generated.yaml", project_root=tmp_path)
    prompt = generated.resolve_path(generated.settings.prompts.final_cleanup).read_text()
    assert "# test final cleanup" in prompt and "未来提示词" not in prompt
    assert "LIBRARY_FRAGMENT" in prompt
    sources = json.loads((job_root / "attachments/library/sources.json").read_text())
    assert len(sources["pages"]) == 4
    assert [p["pdf_page"] for p in sources["pages"]] == [2, 3, 1, 1]
    assert len(library_reference_fragments(prepared[0], job_root / "attachments/library/sources.json")) == 3
    assert "OCR_PAGE_BREAK: 2 -> 3" in prepared[0]
    manifest = json.loads((job_root / "manifest.json").read_text())
    assert manifest["library_material"]["revision_id"] == revision["id"]

    for kind, content_type in (("pdf", "application/pdf"), ("txt", "text/plain"), ("sources", "application/json")):
        download = request_json(app, "GET", f"/api/jobs/{job_id}/library-attachments/{kind}")
        assert download.status_code == 200 and download.headers["content-type"].startswith(content_type)
    assert request_json(app, "GET", f"/api/jobs/{job_id}/library-attachments/state").status_code == 404

    current = app.state.library.get_material(material["id"])
    app.state.library.archive_material(material["id"], base_version=current["version"])
    app.state.library.purge_material(material["id"])
    rerun = run_job_rerun(app=app, job_id=job_id, payload={"start_stage": "prepare-reference"},
                          state_path=job_root / "state.json")
    assert rerun["status"] == "success", rerun
    assert prepared == [prepared[0], prepared[0]]
    app.state.library.close()


@pytest.mark.parametrize("split_empty,sentence_split", [(False, False), (False, True), (True, False), (True, True)])
def test_library_alignment_uses_structured_fragments_not_source_headers(tmp_path, split_empty, sentence_split):
    from api_server import create_app
    write_minimal_settings(tmp_path, segmentation_overrides={"split_on_empty_line": split_empty},
                           reference_overrides={"sentence_split_enabled": sentence_split})
    app = create_app(project_root=tmp_path)
    _, _, revision = prepared_library(app, tmp_path)
    text = app.state.library.revision_file(revision["id"], "txt").read_text()
    sources_path = app.state.library.revision_file(revision["id"], "sources")
    loaded = load_settings(project_root=tmp_path)
    loaded.settings.reference.library_sources_file = str(sources_path)
    blocks = build_reference_blocks(text, loaded)
    assert [b.ref_block_id for b in blocks] == list(range(1, len(blocks) + 1))
    assert all("LIBRARY_FRAGMENT" not in b.reference_text and "PDF第" not in b.reference_text for b in blocks)
    assert all(not ("书1" in b.reference_text and "书2" in b.reference_text) for b in blocks)
    assert all(not ("第一页" in b.reference_text and "第二页" in b.reference_text) for b in blocks)
    loaded.settings.reference.library_sources_file = None
    assert any("LIBRARY_FRAGMENT" in b.reference_text for b in build_reference_blocks(text, loaded))
    app.state.library.close()


def test_new_source_validation_and_scoped_marker_gate(tmp_path):
    from pydantic import ValidationError
    from src.web.models import SingleJobRequest
    base = {"video": "a.wav", "output_dir": "out", "library_material_revision_id": "fixed"}
    with pytest.raises(ValidationError, match="只能选择一种"):
        SingleJobRequest(**base, reference="upload.txt")
    with pytest.raises(ValidationError, match="对谈"):
        SingleJobRequest(**base, content_type="conversation")
    # 旧输入不因书库新增前提而改变。
    assert SingleJobRequest(video="a.wav", output_dir="out").library_material_revision_id is None
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"schema_version": 1, "fragments": [{"header": "来源：书A PDF第2页"}]}))
    require_clean_library_output("# 正文\n\n普通内容", sources)
    for text in ("----- OCR_PAGE_BREAK: 2 -> 3 -----", "LIBRARY_FRAGMENT: 1", "引用了来源：书A PDF第2页"):
        with pytest.raises(ValueError, match="未发布最终稿"):
            require_clean_library_output(text, sources)


def test_library_missing_revision_fails_before_job_is_accepted(tmp_path):
    from api_server import create_app
    write_minimal_settings(tmp_path)
    app = create_app(project_root=tmp_path)
    response = request_json(app, "POST", "/api/jobs", json_body={
        "video": "a.wav", "output_dir": "out", "library_material_revision_id": "missing"})
    assert response.status_code in {400, 404}
    assert not app.state.active_jobs
    assert not list((tmp_path / "data/jobs").glob("*/state.json"))
    app.state.library.close()


def test_history_routes_respect_import_reservation_and_copied_data(tmp_path, monkeypatch):
    from api_server import create_app
    from tests.test_library import pdf
    from tests.test_library_imports import history
    write_minimal_settings(tmp_path)
    source = pdf(tmp_path / "data/uploads/pdf-ocr/group/book.pdf")
    task_id, task_root = history(tmp_path, 99, source, {1: "历史正文"})
    app = create_app(project_root=tmp_path)
    entered, proceed = threading.Event(), threading.Event()
    actual_scan = app.state.library._scan_history
    result = []

    def scan(task_ids=None):
        entered.set()
        assert proceed.wait(3)
        return actual_scan(task_ids)

    monkeypatch.setattr(app.state.library, "_scan_history", scan)
    worker = threading.Thread(target=lambda: result.append(app.state.library.import_history([f"{task_id}:0"])))
    try:
        worker.start()
        assert entered.wait(2)
        for method, path in (("DELETE", f"/api/pdf-book-ocr/{task_id}"),
                             ("POST", f"/api/pdf-book-ocr/{task_id}/retry")):
            response = request_json(app, method, path)
            assert response.status_code == 409 and "导入书库" in response.text
        assert task_root.is_dir() and task_id not in app.state.active_jobs
        proceed.set()
        worker.join(3)
        assert not worker.is_alive()
        book_id = result[0]["items"][0]["book_id"]
        assert request_json(app, "DELETE", f"/api/pdf-book-ocr/{task_id}").status_code == 200
        assert app.state.library.get_page(book_id, 1)["text"] == "历史正文"
    finally:
        proceed.set()
        worker.join(3)
        app.state.library.close()
