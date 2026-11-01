import hashlib
import json
import shutil

import pytest

from src.library import LibraryError, LibraryService
from src.pdf.workflow import build_pdf_ocr_run_identity
from tests.test_library import pdf


def history(root, number, source, texts, *, with_sidecars=True, wrong_source=False):
    task_id = f"pdf-ocr-{number:032x}"
    task = root/'data/jobs/pdf-ocr'/task_id
    model, effort = 'gpt-test', 'high'
    identity = build_pdf_ocr_run_identity(source,model,effort)
    checkpoint = task/'pages'/f'{source.stem}.pages'/identity.fingerprint
    checkpoint.mkdir(parents=True)
    for page,text in texts.items():
        (checkpoint/f'page-{page:06d}.txt').write_text(text,encoding='utf-8')
        if with_sidecars:
            sidecar = {'schema_version':1,'page_number':page if not wrong_source else page+1,
                       'primary_model':model,'model':model,'reasoning_effort':effort,'fallback':False,
                       'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
            (checkpoint/f'page-{page:06d}.source.json').write_text(json.dumps(sidecar),encoding='utf-8')
    state = {'id':task_id,'status':'partial',
             'request_payload':{'input_path':str(source),'ocr_model':model,'ocr_reasoning_effort':effort},
             'items':[{'source_file':source.name,'page_count':3,'completed_pages':len(texts),
                       'ocr_identity':identity.fingerprint if with_sidecars else ''}]}
    (task/'state.json').write_text(json.dumps(state),encoding='utf-8')
    return task_id, task


def test_legacy_partial_import_copy_idempotence_and_manual_protection(tmp_path):
    source = pdf(tmp_path/'data/uploads/pdf-ocr/group/book.pdf')
    task_id, task = history(tmp_path,1,source,{1:'正文',2:''},with_sidecars=False)
    service = LibraryService(tmp_path)
    try:
        candidates = service.history_candidates()
        assert candidates['total'] == 1
        c = candidates['items'][0]
        assert c['reusable_pages'] == 2 and '来源信息不完整' in c['warnings'][0]
        imported = service.import_history([c['id']])['items'][0]
        assert imported['status'] == 'success' and imported['imported_pages'] == 2
        book_id = imported['book_id']
        assert service.get_page(book_id,2)['text'] == ''
        assert service.get_page(book_id,3)['text'] is None
        first = service.get_page(book_id,1)
        manual = service.save_page(book_id,1,text='人工',base_revision_id=first['current_revision_id'])
        again = service.import_history([c['id']])['items'][0]
        assert again['book_id'] == book_id and again['imported_pages'] == 0
        assert service.get_page(book_id,1)['current_revision_id'] == manual['current_revision_id']
        shutil.rmtree(task)
        source.unlink()
        assert service._source_path(book_id).is_file()
        assert (service.root/'ocr'/book_id/f'history-{task_id}-0'/'page-000002.txt').exists()
        assert service.get_page(book_id,2)['text'] == ''
    finally:
        service.close()


def test_import_conflicting_candidates_require_choice_and_bad_sidecar(tmp_path):
    source = pdf(tmp_path/'data/uploads/pdf-ocr/group/book.pdf')
    history(tmp_path,1,source,{1:'A',2:'相同'})
    history(tmp_path,2,source,{1:'B',2:'相同'})
    history(tmp_path,3,source,{3:'错误来源'},wrong_source=True)
    service = LibraryService(tmp_path)
    try:
        candidates = service.history_candidates()['items']
        assert [c['reusable_pages'] for c in candidates] == [2,2,0]
        ids = [c['id'] for c in candidates]
        assert candidates[2]['warnings']
        result = service.import_history(ids[:2])['items']
        assert all('冲突' in ' '.join(r['warnings']) for r in result)
        book_id = result[0]['book_id']
        assert result[1]['book_id'] == book_id
        assert service.get_page(book_id,1)['text'] is None
        assert service.get_page(book_id,2)['text'] == '相同'
        chosen = service.import_history([ids[1]])['items'][0]
        assert chosen['status'] == 'success' and chosen['imported_pages'] == 1
        assert service.get_page(book_id,1)['text'] == 'B'
        assert (service.root/'ocr'/book_id/('history-'+ids[1].replace(':','-'))/'page-000001.txt').is_file()
        service.history_task_active = lambda task_id: True
        with pytest.raises(LibraryError) as err:
            service.import_history([ids[0]])
        assert err.value.status_code == 409
    finally:
        service.close()


def test_history_cannot_escape_upload_root(tmp_path):
    source = pdf(tmp_path/'outside.pdf')
    history(tmp_path,1,source,{1:'不可导入'})
    service = LibraryService(tmp_path)
    try:
        candidate = service.history_candidates()['items'][0]
        assert candidate['reusable_pages'] == 0
        report = service.import_history([candidate['id']])['items'][0]
        assert report['status'] == 'failed'
    finally:
        service.close()


def test_import_progress_is_persisted_and_recover_interrupted(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import src.library.imports as imports
    source = pdf(tmp_path/'data/uploads/pdf-ocr/group/book.pdf')
    history(tmp_path,1,source,{1:'正文'})
    service = LibraryService(tmp_path)
    started, proceed = threading.Event(), threading.Event()
    original = imports.shutil.copy2
    def blocked(src,dst):
        started.set()
        assert proceed.wait(3)
        return original(src,dst)
    try:
        candidate = service.history_candidates()['items'][0]
        monkeypatch.setattr(imports.shutil,'copy2',blocked)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.import_history,[candidate['id']])
            assert started.wait(2)
            job = next(j for j in service.list_jobs()['items'] if j['kind']=='import')
            assert (job['status'],job['completed'],job['total']) == ('running',0,1)
            proceed.set()
            result = future.result(3)
        job = service.get_job(result['job_id'])
        assert (job['status'],job['completed']) == ('success',1)
        assert job['report'][0]['book_id'] == result['items'][0]['book_id']
        with pytest.raises(LibraryError) as err:
            service.resume_job(job['id'])
        assert err.value.status_code == 409
        with service._db(write=True) as db:
            op = service._create_operation(db,'import',1,{'candidate_ids':[candidate['id']]})
        service.recover()
        assert service.get_job(op)['status'] == 'interrupted'
    finally:
        proceed.set(); service.close()
