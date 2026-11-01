from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from pypdf import PdfWriter

from src.web.library import install_library
from tests.helpers import write_minimal_settings
from tests.test_api_server import request_json, request_raw


def pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=300, height=400)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def app(tmp_path: Path):
    write_minimal_settings(tmp_path)
    app = FastAPI()
    install_library(app, tmp_path)
    yield app
    app.state.library.close()


def upload(app, data: bytes, name: str = 'book.pdf') -> dict:
    response = request_raw(app, 'POST', '/api/library/books', content=data, params={'filename': name})
    assert response.status_code == 201, response.text
    return response.json()


def test_library_books_revisions_and_conflicts(app):
    first = upload(app, pdf_bytes(2), 'sample.pdf')
    assert first['title'] == 'sample' and first['page_count'] == 2
    same = upload(app, pdf_bytes(2), 'another-name.pdf')
    assert same['id'] == first['id'] and same['duplicate']
    assert request_raw(app, 'POST', '/api/library/books', content=b'', params={'filename': 'x.pdf'}).status_code == 400
    assert request_raw(app, 'POST', '/api/library/books', content=b'x', params={'filename': 'x.txt'}).status_code == 400
    assert request_raw(app, 'POST', '/api/library/books', content=b'not-pdf', params={'filename': 'bad.pdf'}).status_code == 400

    book_id = first['id']
    assert request_json(app, 'GET', f'/api/library/books/{book_id}/pages/3').status_code == 400
    assert request_json(app, 'GET', f'/api/library/books/{book_id}/pages/0').status_code == 400
    change = {'base_version': first['version'], 'title': 'Example', 'author': 'Author', 'edition': 'first', 'tags': ['history']}
    response = request_json(app, 'PATCH', f'/api/library/books/{book_id}', json_body=change)
    assert response.status_code == 200 and response.json()['title'] == 'Example'
    assert request_json(app, 'PATCH', f'/api/library/books/{book_id}', json_body=change).status_code == 409
    assert request_json(app, 'GET', '/api/library/books', params={'q': 'history'}).json()['total'] == 1

    page_url = f'/api/library/books/{book_id}/pages/1'
    blank = request_json(app, 'PUT', f'{page_url}/text', json_body={'text': '', 'base_revision_id': None})
    assert blank.status_code == 200 and blank.json()['text'] == ''
    assert request_json(app, 'PUT', f'{page_url}/text', json_body={'text': 'old base', 'base_revision_id': None}).status_code == 409
    revision = blank.json()['current_revision_id']
    second = request_json(app, 'PUT', f'{page_url}/text', json_body={'text': '人工文字', 'base_revision_id': revision, 'note': '修订'})
    assert second.status_code == 200
    history = request_json(app, 'GET', f'{page_url}/history').json()['items']
    assert [item['text'] for item in history] == ['人工文字', '']
    detail = request_json(app, 'GET', f'/api/library/books/{book_id}').json()
    assert detail['ready_pages'] == 1 and detail['edited_pages'] == 1 and detail['recognized_pages'] == 0

    preview = request_json(app, 'GET', f'{page_url}/preview')
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
    assert preview.content.startswith(b'\x89PNG')
    cleared = request_json(app, 'DELETE', '/api/library/cache/previews')
    assert cleared.status_code == 200 and cleared.json()['removed_files'] >= 1
    assert request_json(app, 'GET', page_url).json()['text'] == '人工文字'
    archived = request_json(app, 'DELETE', f'/api/library/books/{book_id}', params={'base_version': detail['version']})
    assert archived.status_code == 200 and archived.json()['archived']
    assert request_json(app, 'GET', '/api/library/books').json()['total'] == 0
    assert request_json(app, 'PATCH', f'/api/library/books/{book_id}', json_body={'base_version': archived.json()['version'], 'archived': False}).status_code == 200


def test_material_order_versions_download_and_missing_gate(app):
    one = upload(app, pdf_bytes(2), 'one.pdf')
    two = upload(app, pdf_bytes(3), 'two.pdf')
    ranges = [{'book_id': one['id'], 'start_page': 2, 'end_page': 2},
              {'book_id': two['id'], 'start_page': 1, 'end_page': 1},
              {'book_id': one['id'], 'start_page': 1, 'end_page': 1},
              {'book_id': one['id'], 'start_page': 2, 'end_page': 2}]
    create = request_json(app, 'POST', '/api/library/materials', json_body={'name': '读书会', 'ranges': ranges})
    assert create.status_code == 201
    material = create.json()
    assert material['page_count'] == 4 and material['missing_pages'] == 4 and len(material['duplicate_pages']) == 1
    mid = material['id']
    assert request_json(app, 'POST', f'/api/library/materials/{mid}/revisions', json_body={'base_version': material['version']}).status_code == 409
    for bid, number, text in [(one['id'], 1, ''), (one['id'], 2, '第一页选文'), (two['id'], 1, '第二本')]:
        result = request_json(app, 'PUT', f'/api/library/books/{bid}/pages/{number}/text', json_body={'text': text, 'base_revision_id': None})
        assert result.status_code == 200
    material = request_json(app, 'GET', f'/api/library/materials/{mid}').json()
    assert material['missing_pages'] == 0
    revision = request_json(app, 'POST', f'/api/library/materials/{mid}/revisions', json_body={'base_version': material['version']})
    assert revision.status_code == 201, revision.text
    rid = revision.json()['id']
    source = request_json(app, 'GET', f'/api/library/revisions/{rid}/sources').json()
    assert [(page['book_id'], page['pdf_page']) for page in source['pages']] == [(one['id'], 2), (two['id'], 1), (one['id'], 1), (one['id'], 2)]
    assert request_json(app, 'GET', f'/api/library/revisions/{rid}/pdf').content.startswith(b'%PDF')
    txt = request_json(app, 'GET', f'/api/library/revisions/{rid}/txt').text
    assert '第一页选文' in txt and '第二本' in txt
    assert request_json(app, 'GET', f'/api/library/revisions/{rid}/not-a-kind').status_code == 400
    assert request_json(app, 'PATCH', f'/api/library/materials/{mid}', json_body={'base_version': 999, 'name': '新名称'}).status_code == 409
    assert request_json(app, 'DELETE', f"/api/library/books/{one['id']}", params={'purge': 'true'}).status_code == 409


def test_ocr_options_snapshot_has_no_secret_and_preserves_connection(app, tmp_path, monkeypatch):
    # Stub only the model-triggering boundary: this test never starts OCR.
    settings_file = tmp_path / 'data/jobs/frontend-settings.json'
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({'codex_lb_base_url': 'http://initial.example', 'codex_lb_api_key': 'synthetic-test-key', 'ocr_model': 'example-model'}), encoding='utf-8')
    observed = {}
    def enqueue(book_id, pages, *, options, gateway):
        observed.update(book_id=book_id, pages=pages, options=options, gateway=gateway)
        return {'id': 'job-1', 'status': 'pending'}
    monkeypatch.setattr(app.state.library, 'enqueue_ocr', enqueue)
    response = request_json(app, 'POST', '/api/library/books/book-id/ocr-jobs', json_body={'pages': [1, 3], 'max_concurrency': 4, 'fast_mode': True})
    assert response.status_code == 202, response.text
    assert observed['pages'] == [1, 3]
    assert observed['options']['model'] == 'example-model' and observed['options']['max_concurrency'] == 4
    assert observed['options']['fast_mode'] is True
    assert 'synthetic-test-key' not in json.dumps(observed['options'])
    assert observed['gateway'].connection.base_url == 'http://initial.example'
    assert observed['gateway'].total_timeout is True
    settings_file.write_text(json.dumps({'codex_lb_base_url': 'http://changed.example', 'codex_lb_api_key': 'another-synthetic-key'}), encoding='utf-8')
    restored = app.state.library.gateway_factory(observed['options'])
    assert restored.connection.base_url == 'http://initial.example'
    assert restored.connection.api_key == 'another-synthetic-key'
    assert restored.total_timeout is True
    assert request_json(app, 'POST', '/api/library/books/book-id/ocr-jobs', json_body={'pages': [1], 'max_concurrency': 0}).status_code == 400
    assert request_json(app, 'POST', '/api/library/imports', json_body={'candidate_ids': []}).status_code == 400
