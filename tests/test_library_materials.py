import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from src.library import LibraryError, LibraryService
from tests.test_library import pdf


def ready(service, book, page, text):
    return service.save_page(book['id'],page,text=text,base_revision_id=None)


def test_material_pdf_text_sources_version_and_snapshot(tmp_path):
    service = LibraryService(tmp_path)
    try:
        a = service.add_book(pdf(tmp_path/'a.pdf'),title='书 A',edition='一版')
        b = service.add_book(pdf(tmp_path/'b.pdf',(200,210)),title='书 B')
        first = ready(service,a,1,'第一页🙂')
        ready(service,a,2,'')
        ready(service,a,3,'第三页')
        ready(service,b,2,'第二书')
        ranges = [dict(book_id=a['id'],start_page=1,end_page=2),
                  dict(book_id=b['id'],start_page=2,end_page=2),
                  dict(book_id=a['id'],start_page=1,end_page=1),
                  dict(book_id=a['id'],start_page=3,end_page=3)]
        draft = service.create_material('读书会',ranges)
        assert (draft['page_count'],draft['ready_pages'],draft['missing_pages']) == (5,5,0)
        assert draft['duplicate_pages'][0]['page_number'] == 1
        revision = service.publish_material(draft['id'],base_version=draft['version'])
        sources = json.loads(service.revision_file(revision['id'],'sources').read_text())
        text = service.revision_file(revision['id'],'txt').read_text()
        output = PdfReader(service.revision_file(revision['id'],'pdf'))
        assert [int(p.mediabox.width) for p in output.pages] == [100,110,210,100,120]
        assert [p['pdf_page'] for p in sources['pages']] == [1,2,2,1,3]
        assert [p['fragment_id'] for p in sources['pages']] == [1,1,2,3,4]
        assert 'OCR_PAGE_BREAK: 1 -> 2' in text
        assert sources['pages'][0]['text_revision_id'] == first['current_revision_id']
        for frag in sources['fragments']:
            body = text[frag['text_start']:frag['text_end']]
            assert 'LIBRARY_FRAGMENT' not in body and '来源：' not in body
            assert frag['header'].startswith('----- LIBRARY_FRAGMENT:')
        snapshot = service.snapshot_for_job(revision['id'],tmp_path/'job')
        before = (tmp_path/'job/attachments/library/reference.txt').read_text()
        service.save_page(a['id'],1,text='后来修订',base_revision_id=first['current_revision_id'])
        assert service.get_material_revision(revision['id'])['has_updates']
        assert service.revision_file(revision['id'],'txt').read_text() == before
        assert (tmp_path/'job/attachments/library/reference.txt').read_text() == before
        newer = service.publish_material(draft['id'],base_version=draft['version'])
        assert newer['revision'] == 2 and '后来修订' in service.revision_file(newer['id'],'txt').read_text()
        with pytest.raises(LibraryError) as err:
            service.purge_book(a['id'])
        assert err.value.status_code == 409
        service.purge_material(draft['id'])
        service.purge_book(a['id'])
        assert (tmp_path/'job/attachments/library/reference.pdf').exists()
    finally:
        service.close()


def test_draft_missing_ranges_validation_and_cas(tmp_path):
    service = LibraryService(tmp_path)
    try:
        a = service.add_book(pdf(tmp_path/'a.pdf'))
        draft = service.create_material('草稿',[dict(book_id=a['id'],start_page=1,end_page=2),dict(book_id=a['id'],start_page=2,end_page=2)])
        assert draft['missing_pages'] == 3
        assert len(draft['missing_page_items']) == 2
        with pytest.raises(LibraryError) as err:
            service.publish_material(draft['id'],base_version=1)
        assert err.value.status_code == 409
        with pytest.raises(LibraryError):
            service.update_material(draft['id'],base_version=1,name='错误',ranges=[dict(book_id=a['id'],start_page=3,end_page=1)])
        updated = service.update_material(draft['id'],base_version=1,name='新草稿',ranges=[])
        with pytest.raises(LibraryError) as err:
            service.update_material(draft['id'],base_version=1,name='覆盖',ranges=[])
        assert err.value.status_code == 409 and updated['version']==2
    finally:
        service.close()


def test_snapshot_pins_material_during_copy(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import src.library.materials as materials
    service = LibraryService(tmp_path)
    started, proceed = threading.Event(), threading.Event()
    original_copy = materials.shutil.copy2
    def blocked_copy(src,dst):
        started.set()
        assert proceed.wait(3)
        return original_copy(src,dst)
    try:
        book = service.add_book(pdf(tmp_path/'a.pdf',(100,)))
        ready(service,book,1,'正文')
        material = service.create_material('材料',[dict(book_id=book['id'],start_page=1,end_page=1)])
        revision = service.publish_material(material['id'],base_version=1)
        monkeypatch.setattr(materials.shutil,'copy2',blocked_copy)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.snapshot_for_job,revision['id'],tmp_path/'job')
            assert started.wait(2)
            with pytest.raises(LibraryError) as err:
                service.purge_material(material['id'])
            assert err.value.status_code == 409
            proceed.set()
            snapshot = future.result(3)
        assert Path(snapshot['pdf_path']).is_file()
        service.purge_material(material['id'])
        assert Path(snapshot['pdf_path']).is_file()
    finally:
        proceed.set(); service.close()


def test_material_operation_reports_success_and_failure(tmp_path, monkeypatch):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'a.pdf',(100,)))
        ready(service,book,1,'正文')
        material = service.create_material('材料',[dict(book_id=book['id'],start_page=1,end_page=1)])
        revision = service.publish_material(material['id'],base_version=1)
        operation = service.get_job(revision['job_id'])
        assert (operation['kind'],operation['status'],operation['completed'],operation['total']) == ('material','success',3,3)
        assert operation['revision_id'] == revision['id']
        def fail_write(self, stream):
            raise OSError('模拟磁盘故障')
        monkeypatch.setattr('src.library.materials.PdfWriter.write',fail_write)
        with pytest.raises(LibraryError,match='模拟磁盘故障'):
            service.publish_material(material['id'],base_version=1)
        failed = [j for j in service.list_jobs()['items'] if j['kind']=='material' and j['status']=='failed']
        assert len(failed)==1 and '模拟磁盘故障' in failed[0]['error']
        assert service.list_material_revisions(material['id'])['items'][0]['status'] == 'failed'
    finally:
        service.close()


def test_material_retains_edited_page_newlines_in_unicode_offsets(tmp_path):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
        ready(service,book,1,'\n正文🙂\n\n')
        material = service.create_material('材料',[dict(book_id=book['id'],start_page=1,end_page=1)])
        revision = service.publish_material(material['id'],base_version=1)
        source = json.loads(service.revision_file(revision['id'],'sources').read_text())
        text = service.revision_file(revision['id'],'txt').read_text()
        frag = source['fragments'][0]
        assert text[frag['text_start']:frag['text_end']] == '\n正文🙂\n\n'
    finally:
        service.close()


def test_material_purge_failure_remains_visible_then_restart_finishes(tmp_path, monkeypatch):
    import src.library.materials as materials
    service = LibraryService(tmp_path)
    book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
    ready(service,book,1,'正文')
    material = service.create_material('材料',[dict(book_id=book['id'],start_page=1,end_page=1)])
    revision = service.publish_material(material['id'],base_version=1)
    service.archive_material(material['id'],base_version=1)
    directory = service.root/'materials'/material['id']
    real_rmtree = materials.shutil.rmtree
    def fail_material(path, *args, **kwargs):
        if Path(path) == directory:
            raise PermissionError('材料目录权限不足')
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(materials.shutil,'rmtree',fail_material)
    try:
        with pytest.raises(LibraryError,match='材料目录权限不足'):
            service.purge_material(material['id'])
        item = service.get_material(material['id'])
        assert item['state']=='deleting' and '权限不足' in item['cleanup_error']
        assert any(x['id']==material['id'] for x in service.list_materials()['items'])
        with pytest.raises(LibraryError):
            service.revision_file(revision['id'],'pdf')
        with pytest.raises(LibraryError) as err:
            service.purge_book(book['id'])
        assert err.value.status_code==409 and service._source_path(book['id']).exists()
    finally:
        service.close()
    restarted = LibraryService(tmp_path)
    try:
        restarted.recover()
        assert restarted.get_material(material['id'])['state']=='deleting'
        assert '权限不足' in restarted.get_material(material['id'])['cleanup_error']
        monkeypatch.setattr(materials.shutil,'rmtree',real_rmtree)
        restarted.recover()
        with pytest.raises(LibraryError) as err:
            restarted.get_material(material['id'])
        assert err.value.status_code==404
        assert not directory.exists() and restarted._source_path(book['id']).exists()
    finally:
        restarted.close()


def test_recovery_does_not_remove_book_still_referenced_by_draft(tmp_path):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
        material = service.create_material('保留',[dict(book_id=book['id'],start_page=1,end_page=1)])
        with service._db(write=True) as db:
            db.execute("UPDATE books SET state='deleting' WHERE id=?",(book['id'],))
        service.recover()
        item = service.get_book(book['id'])
        assert item['state']=='deleting' and '草稿引用' in item['cleanup_error']
        assert service._source_path(book['id']).exists()
        assert service.get_material(material['id'])['page_count']==1
    finally:
        service.close()


def test_archived_book_remains_usable_in_material_drafts(tmp_path):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf',(100,110)),title='归档书')
        ready(service,book,1,'第一页')
        ready(service,book,2,'第二页')
        first = dict(book_id=book['id'],start_page=1,end_page=1)
        second = dict(book_id=book['id'],start_page=2,end_page=2)
        draft = service.create_material('原名称',[first,second])
        service.archive_book(book['id'],base_version=book['version'])
        assert service.list_books()['total'] == 0
        updated = service.update_material(draft['id'],base_version=draft['version'],name='新名称',ranges=[second,first])
        assert updated['name'] == '新名称' and updated['ranges'] == [second,first]
        revision = service.publish_material(draft['id'],base_version=updated['version'])
        source = json.loads(service.revision_file(revision['id'],'sources').read_text())
        assert [p['pdf_page'] for p in source['pages']] == [2,1]
        assert '第一页' in service.revision_file(revision['id'],'txt').read_text()
        assert service.create_material('从归档选书',[first])['ready_pages'] == 1
        with pytest.raises(LibraryError) as err:
            service.purge_book(book['id'])
        assert err.value.status_code == 409
    finally:
        service.close()
