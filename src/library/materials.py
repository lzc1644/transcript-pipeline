from __future__ import annotations

import json
import shutil
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from src.ocr_page_markers import format_ocr_page_break
from .store import LibraryError, json_text, new_id, now


class MaterialsMixin:
    def _material_row(self, db, material_id):
        row = db.execute("SELECT * FROM materials WHERE id=? AND state='ready'", (material_id,)).fetchone()
        if row is None:
            raise LibraryError(404, "材料不存在。")
        return row

    def _revision_row(self, db, revision_id, *, ready=True):
        row = db.execute("SELECT * FROM material_revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None or (ready and row["status"] != 'ready'):
            raise LibraryError(404, "材料版本不存在或尚未完成。")
        return row

    def _validated_ranges(self, db, ranges):
        if not isinstance(ranges, list):
            raise LibraryError(400, "选页清单必须为列表。")
        normalized = []
        for item in ranges:
            if not isinstance(item, dict):
                raise LibraryError(400, "选页项格式无效。")
            book_id, start, end = item.get('book_id'), item.get('start_page'), item.get('end_page')
            book = self._book_row(db, book_id)
            if type(start) is not int or type(end) is not int or start < 1 or end < start or end > book['page_count']:
                raise LibraryError(400, "选页范围无效，请检查 PDF 物理页码。")
            normalized.append({'book_id':book_id, 'start_page':start, 'end_page':end})
        return normalized

    def _expand(self, db, ranges, *, include_deleting=False):
        pages = []
        for selected in ranges:
            if include_deleting:
                book = db.execute("SELECT * FROM books WHERE id=?", (selected['book_id'],)).fetchone()
                if book is None:
                    raise LibraryError(404, "材料引用的书籍不存在。")
            else:
                book = self._book_row(db, selected['book_id'])
            for page in range(selected['start_page'], selected['end_page'] + 1):
                state = db.execute("SELECT * FROM pages WHERE book_id=? AND page=?", (book['id'],page)).fetchone()
                pages.append({'book_id':book['id'], 'title':book['title'], 'edition':book['edition'],
                              'pdf_page':page, 'text_revision_id':state['current_revision_id']})
        return pages

    def _material_dict(self, db, row):
        ranges = json.loads(row['ranges'])
        pages = self._expand(db, ranges, include_deleting=True)
        counts = {}
        for p in pages:
            key = (p['book_id'],p['pdf_page'])
            counts[key] = counts.get(key, 0) + 1
        missing = list(dict.fromkeys((p['book_id'],p['pdf_page']) for p in pages if p['text_revision_id'] is None))
        return {'id':row['id'], 'name':row['name'], 'version':row['version'], 'ranges':ranges,
                'page_count':len(pages), 'ready_pages':sum(p['text_revision_id'] is not None for p in pages),
                'missing_pages':sum(p['text_revision_id'] is None for p in pages),
                'missing_page_items':[{'book_id':b,'page_number':p} for b,p in missing],
                'duplicate_pages':[{'book_id':b,'page_number':p,'count':n} for (b,p),n in counts.items() if n > 1],
                'archived':bool(row['archived']), 'state':row['state'], 'cleanup_error':row['cleanup_error'],
                'created_at':row['created_at'], 'updated_at':row['updated_at']}

    @staticmethod
    def _material_name(name):
        if not isinstance(name, str) or not name.strip():
            raise LibraryError(400, "材料名称不能为空。")
        return name.strip()

    def create_material(self, name, ranges=None):
        name = self._material_name(name)
        with self._lock, self._db(write=True) as db:
            selected = self._validated_ranges(db, [] if ranges is None else ranges)
            material_id, stamp = new_id(), now()
            db.execute("INSERT INTO materials(id,name,ranges,created_at,updated_at) VALUES(?,?,?,?,?)", (material_id,name,json_text(selected),stamp,stamp))
            return self._material_dict(db,self._material_row(db,material_id))

    def list_materials(self, archived=False):
        with self._db() as db:
            rows = db.execute("SELECT * FROM materials WHERE state='deleting' OR (archived=? AND state='ready') ORDER BY updated_at DESC,id DESC", (int(archived),)).fetchall()
            return {'items':[self._material_dict(db,row) for row in rows], 'total':len(rows)}

    def get_material(self, material_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            if row is None:
                raise LibraryError(404, "材料不存在。")
            return self._material_dict(db,row)

    def update_material(self, material_id, *, base_version, name, ranges):
        name = self._material_name(name)
        with self._lock, self._db(write=True) as db:
            row = self._material_row(db, material_id)
            if row['version'] != base_version:
                raise LibraryError(409, "材料草稿已被修改，请比较后重新保存。")
            if material_id in self._busy_materials:
                raise LibraryError(409, "材料正在生成，请稍后再修改。")
            selected = self._validated_ranges(db, ranges)
            db.execute("UPDATE materials SET name=?,ranges=?,version=version+1,updated_at=? WHERE id=?", (name,json_text(selected),now(),material_id))
            return self._material_dict(db,self._material_row(db,material_id))

    def archive_material(self, material_id, *, base_version, archived=True):
        with self._lock, self._db(write=True) as db:
            row = self._material_row(db, material_id)
            if row['version'] != base_version:
                raise LibraryError(409, "材料草稿已被修改，请刷新后重试。")
            if material_id in self._busy_materials:
                raise LibraryError(409, "材料正在生成，暂不能归档。")
            db.execute("UPDATE materials SET archived=?,version=version+1,updated_at=? WHERE id=?", (int(archived),now(),material_id))
            return self._material_dict(db,self._material_row(db,material_id))

    def purge_material(self, material_id):
        with self._lock, self._db(write=True) as db:
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            if row is None:
                raise LibraryError(404, "材料不存在。")
            if material_id in self._busy_materials or self._snapshot_pins.get(material_id,0):
                raise LibraryError(409, "材料正在生成或用于创建任务。")
            db.execute("UPDATE materials SET state='deleting',cleanup_error='',updated_at=? WHERE id=?", (now(),material_id))
        try:
            try:
                shutil.rmtree(self.root / 'materials' / material_id)
            except FileNotFoundError:
                pass
        except OSError as exc:
            detail = f"清理材料失败，可重试：{exc}"
            with self._lock, self._db(write=True) as db:
                db.execute("UPDATE materials SET cleanup_error=?,updated_at=? WHERE id=? AND state='deleting'", (detail,now(),material_id))
            raise LibraryError(500, detail) from exc
        with self._lock, self._db(write=True) as db:
            db.execute("DELETE FROM materials WHERE id=? AND state='deleting'", (material_id,))
        return {'success':True}

    def _revision_dict(self, db, row):
        changed = db.execute("""SELECT 1 FROM material_pages m JOIN pages p ON p.book_id=m.book_id AND p.page=m.pdf_page
            WHERE m.revision_id=? AND p.current_revision_id IS NOT m.text_revision_id LIMIT 1""", (row['id'],)).fetchone()
        book_count = db.execute("SELECT COUNT(DISTINCT book_id) FROM material_pages WHERE revision_id=?", (row['id'],)).fetchone()[0]
        return {'id':row['id'], 'material_id':row['material_id'], 'revision':row['revision'], 'name':row['name'],
                'page_count':row['page_count'], 'book_count':book_count, 'status':row['status'], 'created_at':row['created_at'],
                'has_updates':bool(changed), 'error':row['error']}

    def list_material_revisions(self, material_id):
        with self._db() as db:
            self._material_row(db,material_id)
            rows = db.execute("SELECT * FROM material_revisions WHERE material_id=? ORDER BY revision DESC", (material_id,)).fetchall()
            return {'items':[self._revision_dict(db,r) for r in rows], 'total':len(rows)}

    def get_material_revision(self, revision_id):
        with self._db() as db:
            row = self._revision_row(db,revision_id)
            self._material_row(db,row['material_id'])
            return self._revision_dict(db,row)

    def revision_file(self, revision_id, kind):
        if kind not in {'pdf','txt','sources'}:
            raise LibraryError(400, "不支持的材料文件类型。")
        with self._db() as db:
            row = self._revision_row(db,revision_id)
            self._material_row(db,row['material_id'])
            path = self.root / 'materials' / row['material_id'] / str(row['revision']) / {'pdf':'reference.pdf','txt':'reference.txt','sources':'sources.json'}[kind]
            if not path.is_file():
                raise LibraryError(500, "材料版本文件缺失，请重新生成新版。")
            return path

    def publish_material(self, material_id, *, base_version):
        reserved = []
        with self._lock, self._db(write=True) as db:
            material = self._material_row(db,material_id)
            if material['version'] != base_version:
                raise LibraryError(409, "材料草稿已被修改，请刷新后重新生成。")
            if material['archived']:
                raise LibraryError(409, "请先恢复已归档的材料。")
            if material_id in self._busy_materials:
                raise LibraryError(409, "材料正在生成。")
            pages = self._expand(db, json.loads(material['ranges']))
            if not pages:
                raise LibraryError(400, "请先为材料选页。")
            if any(p['text_revision_id'] is None for p in pages):
                raise LibraryError(409, "材料存在缺页，请先识别或填写页面文字。")
            number = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM material_revisions WHERE material_id=?",(material_id,)).fetchone()[0]
            revision_id = new_id()
            db.execute("INSERT INTO material_revisions(id,material_id,revision,name,status,page_count,created_at) VALUES(?,?,?,?,?,?,?)", (revision_id,material_id,number,material['name'],'building',len(pages),now()))
            operation_id = self._create_operation(db,'material',3,{'material_id':material_id,'revision_id':revision_id,'name':material['name']})
            prev = None
            fragment = 0
            for idx,p in enumerate(pages,1):
                if prev is None or p['book_id'] != prev['book_id'] or p['pdf_page'] != prev['pdf_page']+1:
                    fragment += 1
                p['output_page'],p['fragment_id'] = idx,fragment
                db.execute("INSERT INTO material_pages VALUES(?,?,?,?,?,?,?,?)", (revision_id,idx,p['book_id'],p['pdf_page'],p['text_revision_id'],p['title'],p['edition'],fragment))
                prev = p
            # Read immutable text under the same snapshot as page references.
            texts = [db.execute("SELECT text FROM page_revisions WHERE id=?", (p['text_revision_id'],)).fetchone()[0] for p in pages]
            reserved = list(dict.fromkeys(p['book_id'] for p in pages))
            self._busy_materials.add(material_id)
            for book_id in reserved:
                self._busy_books[book_id] = self._busy_books.get(book_id,0)+1
        stage = self.root / 'staging' / new_id()
        try:
            stage.mkdir()
            writer = PdfWriter()
            readers = {}
            for p in pages:
                if p['book_id'] not in readers:
                    readers[p['book_id']] = PdfReader(self._source_path(p['book_id']))
                writer.add_page(readers[p['book_id']].pages[p['pdf_page']-1])
            with (stage / 'reference.pdf').open('wb') as out:
                writer.write(out)
            if len(PdfReader(stage / 'reference.pdf').pages) != len(pages):
                raise ValueError("组合 PDF 页数不一致")
            self._update_operation(operation_id,completed=1)
            content, fragments = self._render_material_text(pages,texts)
            (stage / 'reference.txt').write_text(content,encoding='utf-8',newline='\n')
            self._update_operation(operation_id,completed=2)
            source = {'schema_version':1, 'material_id':material_id, 'revision_id':revision_id,
                      'name':material['name'], 'revision':number, 'pages':pages, 'fragments':fragments}
            (stage / 'sources.json').write_text(json.dumps(source,ensure_ascii=False,indent=2)+'\n', encoding='utf-8',newline='\n')
            destination = self.root / 'materials' / material_id / str(number)
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(destination)
            with self._lock, self._db(write=True) as db:
                db.execute("UPDATE material_revisions SET status='ready' WHERE id=? AND status='building'",(revision_id,))
                db.execute("UPDATE operations SET status='success',completed=3,updated_at=? WHERE id=?",(now(),operation_id))
                result = self._revision_dict(db,self._revision_row(db,revision_id))
                result['job_id'] = operation_id
                return result
        except Exception as exc:
            with self._lock, self._db(write=True) as db:
                db.execute("UPDATE material_revisions SET status='failed',error=? WHERE id=?", (str(exc),revision_id))
                db.execute("UPDATE operations SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc),now(),operation_id))
            if isinstance(exc, LibraryError):
                raise
            raise LibraryError(500, f"材料生成失败：{exc}") from exc
        finally:
            shutil.rmtree(stage,ignore_errors=True)
            with self._lock:
                self._busy_materials.discard(material_id)
                for book_id in reserved:
                    self._busy_books[book_id] -= 1
                    if not self._busy_books[book_id]:
                        del self._busy_books[book_id]

    @staticmethod
    def _render_material_text(pages, texts):
        chunks = []
        length = 0
        fragments = []
        def add(text):
            nonlocal length
            chunks.append(text)
            length += len(text)
        for i, (page, text) in enumerate(zip(pages,texts)):
            text = text.replace('\r\n','\n').replace('\r','\n')
            if i == 0 or page['fragment_id'] != pages[i-1]['fragment_id']:
                if fragments:
                    fragments[-1]['text_end'] = length
                    add('\n\n')
                fragment = {'fragment_id':page['fragment_id'], 'book_id':page['book_id'], 'start_page':page['pdf_page'], 'end_page':page['pdf_page']}
                fragments.append(fragment)
                edition = f" · {page['edition']}" if page['edition'] else ''
                header = f"----- LIBRARY_FRAGMENT: {page['fragment_id']} -----\n来源：《{page['title']}》{edition} · PDF 第 {page['pdf_page']} 页\n\n"
                fragment['header'] = header
                add(header)
                fragment['text_start'] = length
            else:
                add('\n\n' + format_ocr_page_break(pages[i-1]['pdf_page']) + '\n\n')
                fragments[-1]['end_page'] = page['pdf_page']
            add(text)
        if fragments:
            fragments[-1]['text_end'] = length
        return ''.join(chunks), fragments

    def snapshot_for_job(self, revision_id, job_root):
        job_root = Path(job_root)
        with self._lock, self._db() as db:
            revision = self._revision_row(db,revision_id)
            material_id = revision['material_id']
            self._material_row(db,material_id)
            self._snapshot_pins[material_id] = self._snapshot_pins.get(material_id,0)+1
            source = self.root / 'materials' / material_id / str(revision['revision'])
        parent = job_root / 'attachments'
        stage = parent / f'.library-{new_id()}.pending'
        destination = parent / 'library'
        try:
            parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                raise LibraryError(409, "任务已有书库材料快照。")
            stage.mkdir()
            for filename in ('reference.pdf','reference.txt','sources.json'):
                shutil.copy2(source / filename,stage / filename)
            stage.rename(destination)
            return {'revision_id':revision_id, 'material_id':material_id, 'name':revision['name'],
                    'revision':revision['revision'], 'pdf_path':str(destination/'reference.pdf'),
                    'txt_path':str(destination/'reference.txt'), 'sources_path':str(destination/'sources.json')}
        except OSError as exc:
            raise LibraryError(500, f"固定任务材料失败：{exc}") from exc
        finally:
            shutil.rmtree(stage,ignore_errors=True)
            with self._lock:
                self._snapshot_pins[material_id] -= 1
                if not self._snapshot_pins[material_id]:
                    del self._snapshot_pins[material_id]
