from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pypdf import PdfReader


class LibraryError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
 id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 author TEXT NOT NULL, edition TEXT NOT NULL, tags TEXT NOT NULL, page_count INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'ready', cleanup_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
 book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, page INTEGER NOT NULL,
 current_revision_id TEXT, ocr_status TEXT NOT NULL DEFAULT 'missing', error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(book_id,page)
);
CREATE TABLE IF NOT EXISTS page_revisions (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 page INTEGER NOT NULL, revision INTEGER NOT NULL, text TEXT NOT NULL, source_kind TEXT NOT NULL,
 source TEXT NOT NULL, parent_id TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(book_id,page,revision)
);
CREATE TABLE IF NOT EXISTS materials (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 ranges TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'ready',
 cleanup_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_revisions (
 id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
 page_count INTEGER NOT NULL, created_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
 UNIQUE(material_id,revision)
);
CREATE TABLE IF NOT EXISTS material_pages (
 revision_id TEXT NOT NULL REFERENCES material_revisions(id) ON DELETE CASCADE,
 output_page INTEGER NOT NULL, book_id TEXT NOT NULL REFERENCES books(id),
 pdf_page INTEGER NOT NULL, text_revision_id TEXT NOT NULL REFERENCES page_revisions(id),
 title TEXT NOT NULL, edition TEXT NOT NULL, fragment_id INTEGER NOT NULL,
 PRIMARY KEY(revision_id,output_page)
);
CREATE TABLE IF NOT EXISTS ocr_jobs (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 pages TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL, errors TEXT NOT NULL DEFAULT '{}',
 retry_plan TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '[]',
 context TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ocr_page_results (
 job_id TEXT NOT NULL REFERENCES ocr_jobs(id) ON DELETE CASCADE,
 page INTEGER NOT NULL, revision_id TEXT NOT NULL REFERENCES page_revisions(id),
 PRIMARY KEY(job_id,page)
);
CREATE TABLE IF NOT EXISTS history_page_imports (
 candidate_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
 page INTEGER NOT NULL, revision_id TEXT NOT NULL REFERENCES page_revisions(id),
 PRIMARY KEY(candidate_id,book_id,page)
);
CREATE INDEX IF NOT EXISTS page_revisions_page ON page_revisions(book_id,page,revision);
CREATE INDEX IF NOT EXISTS material_pages_book ON material_pages(book_id);
CREATE INDEX IF NOT EXISTS ocr_jobs_status ON ocr_jobs(status,created_at);
"""


class LibraryStore:
    PREVIEW_CACHE_LIMIT = 256 * 1024 * 1024

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / "data/library"
        self.db_path = self.root / "library.sqlite3"
        self._lock = threading.RLock()
        self._busy_books: dict[str, int] = {}
        self._busy_materials: set[str] = set()
        self._snapshot_pins: dict[str, int] = {}
        self._active_import_tasks: set[str] = set()
        self._importing_books: set[str] = set()
        self.gateway_factory = None
        self.history_task_active = lambda task_id: False
        self._schema_ready = False
        self._init_queue()

    def _ensure_storage(self):
        if self._schema_ready:
            return
        with self._lock:
            if self._schema_ready:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            for child in ("books", "ocr", "materials", "cache/previews", "staging"):
                (self.root / child).mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(SCHEMA)
                for table in ('books', 'materials'):
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if 'cleanup_error' not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN cleanup_error TEXT NOT NULL DEFAULT ''")
                conn.commit()
            finally:
                conn.close()
            self._schema_ready = True

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._ensure_storage()
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()


    def _create_operation(self, db, kind, total, context):
        operation_id, stamp = new_id(), now()
        db.execute("INSERT INTO operations(id,kind,status,total,context,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (operation_id,kind,'running',total,json_text(context),stamp,stamp))
        return operation_id

    def _update_operation(self, operation_id, *, completed=None, status=None, error=None, report=None):
        changes, values = ['updated_at=?'], [now()]
        for key, value in (('completed',completed),('status',status),('error',error),('report',report)):
            if value is not None:
                changes.append(f'{key}=?')
                values.append(json_text(value) if key == 'report' else value)
        values.append(operation_id)
        with self._db(write=True) as db:
            db.execute(f"UPDATE operations SET {','.join(changes)} WHERE id=?", values)

    @staticmethod
    def _operation_dict(row):
        return {'id':row['id'], 'kind':row['kind'], 'status':row['status'],
                'total':row['total'], 'completed':row['completed'], 'error':row['error'],
                'report':json.loads(row['report']), **json.loads(row['context']),
                'created_at':row['created_at'], 'updated_at':row['updated_at']}

    def _book_row(self, db, book_id: str):
        row = db.execute("SELECT * FROM books WHERE id=? AND state='ready'", (book_id,)).fetchone()
        if row is None:
            raise LibraryError(404, "书籍不存在。")
        return row

    def _page_row(self, db, book_id: str, page: int):
        book = self._book_row(db, book_id)
        if type(page) is not int or not 1 <= page <= book["page_count"]:
            raise LibraryError(400, "PDF 物理页码超出范围。")
        return db.execute("SELECT * FROM pages WHERE book_id=? AND page=?", (book_id, page)).fetchone()

    def _book_dict(self, db, row):
        stats = db.execute("""SELECT COUNT(current_revision_id) ready,
            SUM(CASE WHEN EXISTS
                (SELECT 1 FROM page_revisions r WHERE r.book_id=pages.book_id AND r.page=pages.page AND r.source_kind='manual')
                THEN 1 ELSE 0 END) edited,
            SUM(CASE WHEN EXISTS
                (SELECT 1 FROM page_revisions r WHERE r.book_id=pages.book_id AND r.page=pages.page AND r.source_kind IN ('ocr','import'))
                THEN 1 ELSE 0 END) recognized,
            SUM(CASE WHEN ocr_status='failed' AND current_revision_id IS NULL THEN 1 ELSE 0 END) failed
            FROM pages WHERE book_id=?""", (row["id"],)).fetchone()
        return {"id": row["id"], "title": row["title"], "author": row["author"],
                "edition": row["edition"], "tags": json.loads(row["tags"]),
                "page_count": row["page_count"], "ready_pages": stats["ready"],
                "edited_pages": stats["edited"] or 0, "recognized_pages": stats["recognized"] or 0,
                "failed_pages": stats["failed"] or 0,
                "version": row["version"], "archived": bool(row["archived"]),
                "state": row["state"], "cleanup_error": row["cleanup_error"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _source_path(self, book_id: str) -> Path:
        return self.root / "books" / book_id / "source.pdf"

    @staticmethod
    def _validate_metadata(title, author, edition, tags):
        if not isinstance(title, str) or not title.strip():
            raise LibraryError(400, "书名不能为空。")
        if not isinstance(author, str) or not isinstance(edition, str):
            raise LibraryError(400, "作者和版本必须是文字。")
        if tags is None:
            tags = []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise LibraryError(400, "标签必须是文字列表。")
        return title.strip(), author.strip(), edition.strip(), [tag.strip() for tag in tags if tag.strip()]

    def add_book(self, pdf_path, *, filename=None, title=None, author='', edition='', tags=None):
        self._ensure_storage()
        path = Path(pdf_path)
        filename = Path(filename or path.name).name
        title, author, edition, tags = self._validate_metadata(title or Path(filename).stem, author, edition, tags)
        stage = self.root / "staging" / new_id()
        stage.mkdir()
        staged = stage / "source.pdf"
        digest = hashlib.sha256()
        try:
            with path.open("rb") as inp, staged.open("xb") as out:
                while chunk := inp.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)
            try:
                page_count = len(PdfReader(staged).pages)
            except Exception as exc:
                raise LibraryError(400, f"PDF 无法读取：{exc}") from exc
            if page_count < 1:
                raise LibraryError(400, "PDF 没有页面。")
            book_id, stamp = new_id(), now()
            with self._lock, self._db(write=True) as db:
                existing = db.execute("SELECT * FROM books WHERE digest=?", (digest.hexdigest(),)).fetchone()
                if existing:
                    if existing["state"] != "ready":
                        raise LibraryError(409, "相同 PDF 正在清理，请稍后重试。")
                    result = self._book_dict(db, existing)
                    result["duplicate"] = True
                    return result
                destination = self.root / "books" / book_id
                stage.rename(destination)
                db.execute("INSERT INTO books(id,digest,title,author,edition,tags,page_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (book_id, digest.hexdigest(), title, author, edition, json_text(tags), page_count, stamp, stamp))
                db.executemany("INSERT INTO pages(book_id,page) VALUES(?,?)", ((book_id, p) for p in range(1, page_count + 1)))
                result = self._book_dict(db, self._book_row(db, book_id))
                result["duplicate"] = False
                return result
        except OSError as exc:
            raise LibraryError(500, f"保存 PDF 失败：{exc}") from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def list_books(self, q='', page=1, page_size=20, archived=False):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
            raise LibraryError(400, "分页参数无效。")
        with self._db() as db:
            where = "(state='deleting' OR (state='ready' AND archived=?)) AND (title LIKE ? OR author LIKE ? OR edition LIKE ? OR tags LIKE ?)"
            term = f"%{q.strip()}%"
            args = (int(archived), term, term, term, term)
            total = db.execute(f"SELECT COUNT(*) FROM books WHERE {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM books WHERE {where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?", (*args, page_size, (page-1)*page_size)).fetchall()
            return {"items": [self._book_dict(db, row) for row in rows], "total": total}

    def get_book(self, book_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise LibraryError(404, "书籍不存在。")
            return self._book_dict(db, row)

    def update_book(self, book_id, *, base_version, title, author, edition, tags):
        title, author, edition, tags = self._validate_metadata(title, author, edition, tags)
        with self._lock, self._db(write=True) as db:
            row = self._book_row(db, book_id)
            if row["version"] != base_version:
                raise LibraryError(409, "书籍资料已被修改，请比较后重新保存。")
            db.execute("UPDATE books SET title=?,author=?,edition=?,tags=?,version=version+1,updated_at=? WHERE id=?",
                       (title, author, edition, json_text(tags), now(), book_id))
            return self._book_dict(db, self._book_row(db, book_id))

    def _assert_book_idle(self, db, book_id):
        if self._busy_books.get(book_id, 0) or db.execute("SELECT 1 FROM ocr_jobs WHERE book_id=? AND status IN ('pending','running') LIMIT 1", (book_id,)).fetchone():
            raise LibraryError(409, "书籍正在识别、导入或生成材料，暂不能执行此操作。")

    def archive_book(self, book_id, *, base_version, archived=True):
        with self._lock, self._db(write=True) as db:
            row = self._book_row(db, book_id)
            if row["version"] != base_version:
                raise LibraryError(409, "书籍资料已被修改，请刷新后重试。")
            self._assert_book_idle(db, book_id)
            db.execute("UPDATE books SET archived=?,version=version+1,updated_at=? WHERE id=?", (int(archived), now(), book_id))
            return self._book_dict(db, self._book_row(db, book_id))

    def purge_book(self, book_id):
        with self._lock, self._db(write=True) as db:
            row = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise LibraryError(404, "书籍不存在。")
            if row["state"] == "ready":
                self._assert_book_idle(db, book_id)
            if db.execute("SELECT 1 FROM material_pages WHERE book_id=? LIMIT 1", (book_id,)).fetchone():
                raise LibraryError(409, "书籍仍被已保存的材料版本引用。")
            for row in db.execute("SELECT ranges FROM materials"):
                if any(item["book_id"] == book_id for item in json.loads(row["ranges"])):
                    raise LibraryError(409, "书籍仍被材料草稿引用。")
            db.execute("UPDATE books SET state='deleting',cleanup_error='',updated_at=? WHERE id=?", (now(),book_id))
        try:
            for directory in (self.root / "books" / book_id, self.root / "ocr" / book_id):
                try:
                    shutil.rmtree(directory)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            detail = f"清理书籍文件失败，可重试：{exc}"
            with self._lock, self._db(write=True) as db:
                db.execute("UPDATE books SET cleanup_error=?,updated_at=? WHERE id=? AND state='deleting'", (detail,now(),book_id))
            raise LibraryError(500, detail) from exc
        with self._lock, self._db(write=True) as db:
            db.execute("DELETE FROM books WHERE id=? AND state='deleting'", (book_id,))
        return {"success": True}

    def _page_dict(self, db, row):
        rev = db.execute("SELECT * FROM page_revisions WHERE id=?", (row["current_revision_id"],)).fetchone() if row["current_revision_id"] else None
        return {"page_number": row["page"], "text": rev["text"] if rev else None,
                "current_revision_id": rev["id"] if rev else None,
                "source_kind": rev["source_kind"] if rev else None,
                "ocr_status": row["ocr_status"], "error": row["error"]}

    def get_page(self, book_id, page):
        with self._db() as db:
            return self._page_dict(db, self._page_row(db, book_id, page))

    def page_history(self, book_id, page):
        with self._db() as db:
            self._page_row(db, book_id, page)
            rows = db.execute("SELECT * FROM page_revisions WHERE book_id=? AND page=? ORDER BY revision DESC", (book_id,page)).fetchall()
            items = [{"id":r["id"], "revision":r["revision"], "text":r["text"], "source_kind":r["source_kind"],
                      "source":json.loads(r["source"]), "parent_id":r["parent_id"], "note":r["note"], "created_at":r["created_at"]} for r in rows]
            return {"items":items, "total":len(items)}

    def _append_revision(self, db, book_id, page, text, source_kind, source, parent_id=None, note='', make_current=True):
        rev_id = new_id()
        number = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM page_revisions WHERE book_id=? AND page=?", (book_id,page)).fetchone()[0]
        db.execute("INSERT INTO page_revisions VALUES(?,?,?,?,?,?,?,?,?,?)", (rev_id, book_id, page, number, text, source_kind, json_text(source), parent_id, note, now()))
        if make_current:
            db.execute("UPDATE pages SET current_revision_id=?,ocr_status='complete',error='' WHERE book_id=? AND page=?", (rev_id,book_id,page))
        db.execute("UPDATE books SET updated_at=? WHERE id=?", (now(),book_id))
        return rev_id

    def save_page(self, book_id, page, *, text, base_revision_id, note=''):
        if not isinstance(text, str) or not isinstance(note, str):
            raise LibraryError(400, "页面文字及修订说明必须为文字。")
        with self._lock, self._db(write=True) as db:
            row = self._page_row(db, book_id, page)
            if row["current_revision_id"] != base_revision_id:
                raise LibraryError(409, "页面已有新修订，请比较后重新保存。")
            self._append_revision(db, book_id, page, text, 'manual', {}, base_revision_id, note)
            return self._page_dict(db, self._page_row(db, book_id, page))

    def preview_page(self, book_id, page):
        with self._lock, self._db() as db:
            self._page_row(db, book_id, page)
            self._busy_books[book_id] = self._busy_books.get(book_id, 0) + 1
        try:
            cache = self.root / "cache/previews"
            target = cache / f"{book_id}-{page}.png"
            with self._lock:
                if target.exists():
                    os.utime(target, None)
                    return target
            stage = self.root / "staging" / new_id()
            stage.mkdir()
            try:
                proc = subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1600", "-png", str(self._source_path(book_id)), str(stage / 'preview')], capture_output=True, text=True)
                result = stage / "preview.png"
                if proc.returncode or not result.is_file():
                    raise LibraryError(500, f"页面预览渲染失败：{proc.stderr.strip() or '未生成图像'}")
                with self._lock:
                    result.replace(target)
            except FileNotFoundError as exc:
                raise LibraryError(500, "页面预览不可用：未安装 pdftoppm。") from exc
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            with self._lock:
                images = sorted((p for p in cache.glob('*.png') if p.is_file()), key=lambda p:p.stat().st_mtime)
                total = sum(p.stat().st_size for p in images)
                for image in images:
                    if total <= self.PREVIEW_CACHE_LIMIT:
                        break
                    if image == target:
                        continue
                    size = image.stat().st_size
                    image.unlink(missing_ok=True)
                    total -= size
            return target
        finally:
            with self._lock:
                self._busy_books[book_id] -= 1
                if not self._busy_books[book_id]:
                    del self._busy_books[book_id]

    def clear_preview_cache(self):
        self._ensure_storage()
        removed_files = removed_bytes = 0
        with self._lock:
            for path in (self.root / "cache/previews").glob("*.png"):
                try:
                    size = path.stat().st_size
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise LibraryError(500, f"清理页面预览缓存失败：{exc}") from exc
                removed_files += 1
                removed_bytes += size
        return {"removed_files":removed_files, "removed_bytes":removed_bytes}
