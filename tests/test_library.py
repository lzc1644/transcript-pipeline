from pathlib import Path

import pytest
from pypdf import PdfWriter

from src.library import LibraryError, LibraryService


def pdf(path: Path, widths=(100, 110, 120)):
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=200)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.write(path)
    return path


def test_books_deduplicate_search_versions_and_page_editing(tmp_path):
    service = LibraryService(tmp_path)
    try:
        first = service.add_book(pdf(tmp_path/'a.pdf'),title='哲学',author='作者',edition='第1版',tags=['讨论'])
        duplicate = service.add_book(tmp_path/'a.pdf', title='另名')
        other = service.add_book(pdf(tmp_path/'b.pdf',(200,)),title='哲学',edition='第2版')
        assert duplicate['id'] == first['id'] and duplicate['duplicate']
        assert other['id'] != first['id']
        assert service.list_books(q='作者')['total'] == 1
        assert service.list_books(q='讨论')['total'] == 1
        assert service.list_books(page=1,page_size=1)['total'] == 2
        assert service.get_page(first['id'],1)['text'] is None
        blank = service.save_page(first['id'],1,text='',base_revision_id=None,note='空白页')
        assert blank['text'] == '' and blank['current_revision_id']
        with pytest.raises(LibraryError) as err:
            service.save_page(first['id'],1,text='不应覆盖',base_revision_id=None)
        assert err.value.status_code == 409
        updated = service.save_page(first['id'],1,text='修订',base_revision_id=blank['current_revision_id'])
        history = service.page_history(first['id'],1)
        assert history['total'] == 2 and history['items'][1]['text'] == ''
        assert service.get_book(first['id'])['edited_pages'] == 1
        assert updated['current_revision_id'] != blank['current_revision_id']
        with pytest.raises(LibraryError) as err:
            service.get_page(first['id'],0)
        assert err.value.status_code == 400
        changed = service.update_book(first['id'],base_version=1,title='新标题',author='A',edition='E',tags=[])
        with pytest.raises(LibraryError) as err:
            service.update_book(first['id'],base_version=1,title='旧',author='',edition='',tags=[])
        assert err.value.status_code == 409
        archived = service.archive_book(first['id'],base_version=changed['version'])
        assert service.add_book(tmp_path/'a.pdf')['archived']
        assert service.list_books()['total'] == 1 and service.list_books(archived=True)['total'] == 1
        assert not service.archive_book(first['id'],base_version=archived['version'],archived=False)['archived']
    finally:
        service.close()


def test_preview_and_invalid_pdf(tmp_path):
    service = LibraryService(tmp_path)
    try:
        path = tmp_path/'not.pdf'; path.write_text('no PDF')
        with pytest.raises(LibraryError) as err:
            service.add_book(path)
        assert err.value.status_code == 400
        book = service.add_book(pdf(tmp_path/'valid.pdf'))
        preview = service.preview_page(book['id'],2)
        assert preview.read_bytes().startswith(b'\x89PNG')
        assert service.preview_page(book['id'],2) == preview
    finally:
        service.close()


def test_construction_and_unused_close_are_pure(tmp_path):
    service = LibraryService(tmp_path)
    assert not (tmp_path/'data/library').exists()
    service.close()
    assert not (tmp_path/'data/library').exists()
    other = LibraryService(tmp_path)
    try:
        assert other.list_books()['total'] == 0
        assert (tmp_path/'data/library/library.sqlite3').is_file()
    finally:
        other.close()


def test_concurrent_upload_and_edit_conflicts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service = LibraryService(tmp_path)
    try:
        source = pdf(tmp_path/'a.pdf')
        with ThreadPoolExecutor(max_workers=2) as pool:
            books = list(pool.map(lambda _:service.add_book(source), range(2)))
        assert len({b['id'] for b in books}) == 1
        assert sorted(b['duplicate'] for b in books) == [False,True]
        book = books[0]
        def save(value):
            try:
                return service.save_page(book['id'],1,text=value,base_revision_id=None)
            except LibraryError as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save,['第一版','第二版']))
        assert sum(r == 409 for r in results) == 1
        assert service.page_history(book['id'],1)['total'] == 1
    finally:
        service.close()


def test_clear_preview_cache_only_removes_previews(tmp_path):
    service = LibraryService(tmp_path)
    try:
        book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
        service.save_page(book['id'],1,text='保留',base_revision_id=None)
        preview = service.preview_page(book['id'],1)
        size = preview.stat().st_size
        assert service.clear_preview_cache() == {'removed_files':1,'removed_bytes':size}
        assert not preview.exists()
        assert service._source_path(book['id']).is_file()
        assert service.get_page(book['id'],1)['text'] == '保留'
        assert service.clear_preview_cache() == {'removed_files':0,'removed_bytes':0}
    finally:
        service.close()


def test_book_purge_failure_is_visible_and_restart_retries_all_files(tmp_path, monkeypatch):
    import src.library.store as store
    service = LibraryService(tmp_path)
    book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
    ocr = service.root/'ocr'/book['id']
    ocr.mkdir(parents=True)
    (ocr/'checkpoint.txt').write_text('OCR 原始资料')
    real_rmtree = store.shutil.rmtree
    def fail_ocr(path, *args, **kwargs):
        if Path(path) == ocr:
            raise PermissionError('OCR 目录权限不足')
        return real_rmtree(path, *args, **kwargs)
    monkeypatch.setattr(store.shutil,'rmtree',fail_ocr)
    try:
        with pytest.raises(LibraryError,match='OCR 目录权限不足'):
            service.purge_book(book['id'])
        assert not service._source_path(book['id']).exists()
        assert (ocr/'checkpoint.txt').exists()
        item = service.get_book(book['id'])
        assert item['state']=='deleting' and '权限不足' in item['cleanup_error']
        assert any(x['id']==book['id'] for x in service.list_books()['items'])
        with pytest.raises(LibraryError):
            service.get_page(book['id'],1)
    finally:
        service.close()
    restarted = LibraryService(tmp_path)
    try:
        restarted.recover()
        item = restarted.get_book(book['id'])
        assert item['state']=='deleting' and '权限不足' in item['cleanup_error']
        assert (ocr/'checkpoint.txt').exists()
        monkeypatch.setattr(store.shutil,'rmtree',real_rmtree)
        restarted.recover()
        assert not ocr.exists()
        with pytest.raises(LibraryError) as err:
            restarted.get_book(book['id'])
        assert err.value.status_code == 404
    finally:
        restarted.close()


def test_existing_library_schema_gains_cleanup_error_columns(tmp_path):
    import sqlite3
    service = LibraryService(tmp_path)
    book = service.add_book(pdf(tmp_path/'book.pdf',(100,)))
    material = service.create_material('草稿')
    service.close()
    with sqlite3.connect(service.db_path) as db:
        db.execute('ALTER TABLE books DROP COLUMN cleanup_error')
        db.execute('ALTER TABLE materials DROP COLUMN cleanup_error')
    restarted = LibraryService(tmp_path)
    try:
        assert restarted.get_book(book['id'])['cleanup_error'] == ''
        assert restarted.get_material(material['id'])['cleanup_error'] == ''
    finally:
        restarted.close()
