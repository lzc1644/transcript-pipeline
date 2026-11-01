import json
import threading
import time

import pytest

from src.library import LibraryService
from src.pdf.ocr import PDFOCRPagesResult
from tests.test_library import pdf


def wait_job(service, job_id):
    until = time.monotonic() + 4
    while time.monotonic() < until:
        job = service.get_job(job_id)
        if job['status'] in {'success','partial','failed','interrupted'}:
            return job
        time.sleep(.01)
    pytest.fail('OCR 作业未结束')


def options():
    return {'model':'gpt-test','reasoning_effort':'high','fast_mode':False,'max_concurrency':2,
            'submit_interval_seconds':0,'timeout_seconds':10,
            'connection':{'base_url':'https://example.test','api_key_env':'CODEX_LB_API_KEY','api_key':'secret'}}


def test_overlapping_jobs_serial_and_manual_not_overwritten(tmp_path, monkeypatch):
    service = LibraryService(tmp_path)
    started, proceed = threading.Event(), threading.Event()
    calls = []
    def fake(source, pages, opts, **kwargs):
        assert kwargs['prompt_builder'].__name__ == 'build_pdf_book_ocr_image_prompt'
        calls.append(list(pages)); started.set(); assert proceed.wait(3)
        for page in pages:
            kwargs['page_callback'](page, f'OCR {page}', {'page_number':page})
        return PDFOCRPagesResult(page_count=3,texts_by_page={p:f'OCR {p}' for p in pages})
    monkeypatch.setattr('src.pdf.ocr.ocr_pdf_pages',fake)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf'))
        first = service.enqueue_ocr(book['id'],[1,2],options=options(),gateway=object())
        assert started.wait(2)
        second = service.enqueue_ocr(book['id'],[2,3],options=options(),gateway=object())
        manual = service.save_page(book['id'],1,text='人工',base_revision_id=None)
        proceed.set()
        assert wait_job(service,first['id'])['status'] == 'success'
        assert wait_job(service,second['id'])['status'] == 'success'
        assert calls == [[1,2],[3]]
        assert service.get_page(book['id'],1)['text'] == '人工'
        history = service.page_history(book['id'],1)['items']
        assert {r['source_kind'] for r in history} == {'manual','ocr'}
        assert service.get_page(book['id'],1)['current_revision_id'] == manual['current_revision_id']
        assert 'secret' not in service.db_path.read_bytes().decode('latin1')
    finally:
        proceed.set(); service.close()


def test_interrupted_checkpoint_recovered_without_gateway_or_ocr(tmp_path, monkeypatch):
    service = LibraryService(tmp_path)
    book = service.add_book(pdf(tmp_path/'book.pdf'))
    # Simulate power loss after page checkpoint publication, before DB commit.
    job_id = 'a'*32
    with service._db(write=True) as db:
        db.execute("INSERT INTO ocr_jobs(id,book_id,pages,options,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (job_id,book['id'],json.dumps([1]),json.dumps(options()),'running','2020','2020'))
    checkpoint = service.root/'ocr'/book['id']/job_id
    checkpoint.mkdir(parents=True)
    (checkpoint/'page-000001.txt').write_text('')
    service.close()
    resumed = LibraryService(tmp_path)
    try:
        monkeypatch.setattr('src.pdf.ocr.ocr_pdf_pages',lambda *a,**kw:pytest.fail('不得产生 OCR 请求'))
        resumed.recover()
        assert resumed.get_job(job_id)['status'] == 'interrupted'
        assert resumed.get_page(book['id'],1)['text'] == ''
        again = resumed.enqueue_ocr(book['id'],[1],options=options())
        assert wait_job(resumed,again['id'])['status'] == 'success'
        resumed.resume_job(job_id)
        assert wait_job(resumed,job_id)['status'] == 'success'
    finally:
        resumed.close()


def test_interrupted_job_stops_queue_until_explicit_resume(tmp_path, monkeypatch):
    service = LibraryService(tmp_path)
    started = threading.Event()
    seen = []
    def fake(source,pages,opts,**kwargs):
        started.set()
        while not kwargs['stop_event'].wait(.01):
            pass
        seen.append(list(pages))
        kwargs['page_callback'](pages[0],'已完成',{'page_number':pages[0]})
        return PDFOCRPagesResult(page_count=3,texts_by_page={pages[0]:'已完成'},interrupted=True)
    monkeypatch.setattr('src.pdf.ocr.ocr_pdf_pages',fake)
    book = service.add_book(pdf(tmp_path/'book.pdf'))
    first = service.enqueue_ocr(book['id'],[1,2],options=options(),gateway=object())
    assert started.wait(2)
    second = service.enqueue_ocr(book['id'],[2,3],options=options(),gateway=object())
    service.close()
    new = LibraryService(tmp_path)
    try:
        new.recover()
        assert new.get_job(first['id'])['status'] == 'interrupted'
        assert new.get_job(second['id'])['status'] == 'interrupted'
        assert new.get_page(book['id'],1)['text'] == '已完成'
        assert seen == [[1,2]]
    finally:
        new.close()


def test_same_service_can_restart_without_auto_dispatch(tmp_path, monkeypatch):
    service = LibraryService(tmp_path)
    service.recover()
    service.close()
    service.recover()
    try:
        calls = []
        def fake(source,pages,opts,**kwargs):
            calls.append(list(pages))
            kwargs['page_callback'](1,'重启后',{'page_number':1})
            return PDFOCRPagesResult(page_count=1,texts_by_page={1:'重启后'})
        monkeypatch.setattr('src.pdf.ocr.ocr_pdf_pages',fake)
        book = service.add_book(pdf(tmp_path/'one.pdf',(100,)))
        job = service.enqueue_ocr(book['id'],[1],options=options(),gateway=object())
        assert wait_job(service,job['id'])['status'] == 'success'
        assert calls == [[1]]
    finally:
        service.close()


def test_recover_keeps_valid_checkpoint_if_another_has_invalid_source(tmp_path):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf'))
        job_id = 'b'*32
        with service._db(write=True) as db:
            db.execute("INSERT INTO ocr_jobs(id,book_id,pages,options,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (job_id,book['id'],json.dumps([1,2]),json.dumps(options()),'running','2020','2020'))
        checkpoint = service.root/'ocr'/book['id']/job_id
        checkpoint.mkdir(parents=True)
        (checkpoint/'page-000001.txt').write_text('已完成')
        (checkpoint/'page-000002.txt').write_text('不能确认')
        (checkpoint/'page-000002.source.json').write_text('{"page_number":3}',encoding='utf-8')
        service.recover()
        assert service.get_page(book['id'],1)['text'] == '已完成'
        assert service.get_page(book['id'],2)['text'] is None
        job = service.get_job(job_id)
        assert job['status'] == 'interrupted' and '检查点读取失败' in job['error']
    finally:
        service.close()
