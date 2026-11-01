from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from src.pdf.ocr import load_codex_ocr_page_checkpoints, load_codex_ocr_page_sources
from src.pdf.workflow import PDFOCRRunIdentity, PDFOCRRetryPlan, build_pdf_ocr_retry_plan
from .store import LibraryError, json_text, new_id, now


class JobsMixin:
    def _init_queue(self):
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='library-ocr')
        self._worker_running = False
        self._gateways = {}

    def recover(self):
        with self._lock:
            if self._stop.is_set():
                self._init_queue()
        with self._lock, self._db(write=True) as db:
            db.execute("UPDATE ocr_jobs SET status='interrupted',updated_at=?,error='服务已重启，请手动继续。' WHERE status IN ('pending','running')", (now(),))
            db.execute("UPDATE pages SET ocr_status='missing' WHERE current_revision_id IS NULL AND ocr_status IN ('queued','running')")
            db.execute("UPDATE material_revisions SET status='failed',error='生成中断，请重新生成。' WHERE status='building'")
            db.execute("UPDATE operations SET status='interrupted',error='服务已重启，请重新操作。',updated_at=? WHERE status IN ('pending','running')", (now(),))
            materials = [row['id'] for row in db.execute("SELECT id FROM materials WHERE state='deleting'")]
            books = [row['id'] for row in db.execute("SELECT id FROM books WHERE state='deleting'")]
        # These objects were already approved for permanent deletion. Complete the
        # interrupted operation, preserving any references that still protect a book.
        for table, identifiers, purge in (('materials', materials, self.purge_material), ('books', books, self.purge_book)):
            for identifier in identifiers:
                try:
                    purge(identifier)
                except LibraryError as exc:
                    with self._db(write=True) as db:
                        db.execute(f"UPDATE {table} SET cleanup_error=?,updated_at=? WHERE id=? AND state='deleting'", (exc.detail, now(), identifier))
        with self._db() as db:
            interrupted = [dict(row) for row in db.execute("SELECT * FROM ocr_jobs WHERE status='interrupted'")]
        # A checkpoint can reach disk just before the page revision transaction.
        # Recover these successful pages before any new job computes its missing set.
        for job in interrupted:
            try:
                self._ingest_saved(job)
            except (OSError, ValueError) as exc:
                with self._db(write=True) as db:
                    db.execute("UPDATE ocr_jobs SET error=? WHERE id=?", (f"检查点读取失败：{exc}", job['id']))

    def close(self):
        self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        if not self._schema_ready:
            return
        with self._lock, self._db(write=True) as db:
            db.execute("UPDATE ocr_jobs SET status='interrupted',updated_at=?,error='服务已停止，请手动继续。' WHERE status IN ('pending','running')", (now(),))
            db.execute("UPDATE pages SET ocr_status='missing' WHERE current_revision_id IS NULL AND ocr_status IN ('queued','running')")
        self._gateways.clear()

    @staticmethod
    def _options(options):
        if not isinstance(options,dict):
            raise LibraryError(400,"OCR 配置无效。")
        model = options.get('model')
        if not isinstance(model,str) or not model.strip():
            raise LibraryError(400,"OCR 模型不能为空。")
        effort = options.get('reasoning_effort','')
        fast = options.get('fast_mode',False)
        concurrency = options.get('max_concurrency',1)
        interval = options.get('submit_interval_seconds',0)
        timeout = options.get('timeout_seconds',480)
        if not isinstance(effort,str) or type(fast) is not bool or type(concurrency) is not int or concurrency < 1:
            raise LibraryError(400,"OCR 模型设置或并发数无效。")
        if not isinstance(interval,(int,float)) or interval < 0 or not isinstance(timeout,(int,float)) or timeout <= 0:
            raise LibraryError(400,"OCR 间隔或超时时间无效。")
        connection = options.get('connection',{})
        if not isinstance(connection,dict):
            raise LibraryError(400,"OCR 连接配置无效。")
        allowed = ('base_url','responses_path','codex_responses_path','files_create_path','files_finalize_path_template','api_key_env','bypass_proxy')
        connection = {key:connection[key] for key in allowed if key in connection}
        url = connection.get('base_url', '')
        if not isinstance(url,str):
            raise LibraryError(400,"OCR 网关地址无效。")
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LibraryError(400,"OCR 网关地址不能包含凭据、查询参数或片段。")
        return {'model':model.strip(), 'reasoning_effort':effort.strip(), 'fast_mode':fast,
                'max_concurrency':concurrency, 'submit_interval_seconds':interval,
                'timeout_seconds':timeout, 'connection':connection}

    def _job_dict(self, db, row):
        pages = json.loads(row['pages'])
        completed = failed = 0
        for page in pages:
            state = db.execute("SELECT current_revision_id,ocr_status FROM pages WHERE book_id=? AND page=?", (row['book_id'],page)).fetchone()
            if state and state['current_revision_id'] is not None:
                completed += 1
            elif state and state['ocr_status'] == 'failed':
                failed += 1
        return {'id':row['id'], 'kind':'ocr', 'book_id':row['book_id'], 'status':row['status'], 'pages':pages,
                'total':len(pages), 'completed':completed,
                'total_pages':len(pages), 'completed_pages':completed, 'failed_pages':failed,
                'errors':json.loads(row['errors']), 'error':row['error'],
                'created_at':row['created_at'], 'updated_at':row['updated_at'], 'options':json.loads(row['options'])}

    def list_jobs(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM ocr_jobs").fetchall()
            operations = db.execute("SELECT * FROM operations").fetchall()
            items = [self._job_dict(db,r) for r in rows] + [self._operation_dict(r) for r in operations]
            items.sort(key=lambda item:(item['created_at'],item['id']),reverse=True)
            return {'items':items, 'total':len(items)}

    def get_job(self, job_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM ocr_jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None:
                return self._job_dict(db,row)
            operation = db.execute("SELECT * FROM operations WHERE id=?",(job_id,)).fetchone()
            if operation is not None:
                return self._operation_dict(operation)
            raise LibraryError(404,"书库作业不存在。")

    def enqueue_ocr(self, book_id, pages, *, options, gateway=None):
        options = self._options(options)
        with self._lock, self._db(write=True) as db:
            book = self._book_row(db,book_id)
            if book['archived']:
                raise LibraryError(409,"已归档书籍不能识别，请先恢复。")
            if book_id in self._importing_books:
                raise LibraryError(409,"书籍正在导入历史页面，请稍后识别。")
            if self._stop.is_set():
                raise LibraryError(409,"书库服务正在停止。")
            if pages is None:
                selected = [r['page'] for r in db.execute("SELECT page FROM pages WHERE book_id=? AND current_revision_id IS NULL ORDER BY page",(book_id,))]
            else:
                if not isinstance(pages,list) or any(type(p) is not int or p < 1 or p > book['page_count'] for p in pages):
                    raise LibraryError(400,"OCR 物理页码无效。")
                selected = sorted(set(pages))
            job_id, stamp = new_id(), now()
            status = 'pending' if selected else 'success'
            db.execute("INSERT INTO ocr_jobs(id,book_id,pages,options,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id,book_id,json_text(selected),json_text(options),status,stamp,stamp))
            if gateway is not None:
                self._gateways[job_id] = gateway
            result = self._job_dict(db,db.execute("SELECT * FROM ocr_jobs WHERE id=?",(job_id,)).fetchone())
        if selected:
            self._schedule()
        return result

    def resume_job(self, job_id, *, gateway=None):
        with self._lock, self._db(write=True) as db:
            row = db.execute("SELECT * FROM ocr_jobs WHERE id=?",(job_id,)).fetchone()
            if row is None:
                if db.execute("SELECT 1 FROM operations WHERE id=?",(job_id,)).fetchone():
                    raise LibraryError(409,"导入或材料生成需重新操作，不能继续该作业。")
                raise LibraryError(404,"OCR 作业不存在。")
            if row['status'] not in {'interrupted','failed','partial'}:
                raise LibraryError(409,"当前 OCR 作业不能继续。")
            book = self._book_row(db,row['book_id'])
            if book['archived'] or self._stop.is_set():
                raise LibraryError(409,"书籍已归档或书库正在停止，不能继续。")
            if row['book_id'] in self._importing_books:
                raise LibraryError(409,"书籍正在导入历史页面，请稍后继续。")
            options = json.loads(row['options'])
            identity = PDFOCRRunIdentity(book['digest'],options['model'],options['reasoning_effort'])
            errors = {int(k):v for k,v in json.loads(row['errors']).items()}
            previous = PDFOCRRetryPlan.from_dict(json.loads(row['retry_plan'])) if row['retry_plan'] else None
            plan = build_pdf_ocr_retry_plan(identity, errors, previous)
            db.execute("UPDATE ocr_jobs SET status='pending',retry_plan=?,error='',updated_at=? WHERE id=?", (json_text(plan.to_dict()),now(),job_id))
            if gateway is not None:
                self._gateways[job_id] = gateway
            result = self._job_dict(db,db.execute("SELECT * FROM ocr_jobs WHERE id=?",(job_id,)).fetchone())
        self._schedule()
        return result

    def _schedule(self):
        with self._lock:
            if not self._worker_running and not self._stop.is_set():
                self._worker_running = True
                self._executor.submit(self._drain)

    def _drain(self):
        released = False
        try:
            while not self._stop.is_set():
                with self._lock, self._db(write=True) as db:
                    job = db.execute("SELECT * FROM ocr_jobs WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
                    if job is None:
                        self._worker_running = False
                        released = True
                        return
                    db.execute("UPDATE ocr_jobs SET status='running',updated_at=? WHERE id=? AND status='pending'", (now(),job['id']))
                try:
                    self._run_job(dict(job))
                except Exception as exc:
                    with self._lock, self._db(write=True) as db:
                        status = 'interrupted' if self._stop.is_set() else 'failed'
                        db.execute("UPDATE ocr_jobs SET status=?,error=?,updated_at=? WHERE id=?", (status,str(exc),now(),job['id']))
                        db.execute("UPDATE pages SET ocr_status='failed',error=? WHERE book_id=? AND ocr_status='running' AND current_revision_id IS NULL", (str(exc),job['book_id']))
                finally:
                    self._gateways.pop(job['id'],None)
        finally:
            if not released:
                with self._lock:
                    self._worker_running = False

    def _record_ocr_page(self, job, page, text, source):
        with self._lock, self._db(write=True) as db:
            row = self._page_row(db,job['book_id'],page)
            if db.execute("SELECT 1 FROM ocr_page_results WHERE job_id=? AND page=?",(job['id'],page)).fetchone():
                return
            rev = self._append_revision(db,job['book_id'],page,text,'ocr',{'job_id':job['id'], **source},row['current_revision_id'],make_current=row['current_revision_id'] is None)
            db.execute("INSERT INTO ocr_page_results(job_id,page,revision_id) VALUES(?,?,?)", (job['id'],page,rev))

    def _ingest_saved(self, job):
        options, pages = json.loads(job['options']), json.loads(job['pages'])
        checkpoint = self.root / 'ocr' / job['book_id'] / job['id']
        if not checkpoint.is_dir():
            return
        with self._db() as db:
            book = self._book_row(db,job['book_id'])
            page_count = book['page_count']
        saved = load_codex_ocr_page_checkpoints(checkpoint,page_count)
        invalid = []
        for page,text in saved.items():
            if page not in pages:
                continue
            try:
                load_codex_ocr_page_sources(checkpoint,{page:text},options['model'])
                source_path = checkpoint / f'page-{page:06d}.source.json'
                if source_path.exists():
                    meta = json.loads(source_path.read_text(encoding='utf-8'))
                    if meta.get('page_number') != page:
                        raise ValueError(f"OCR 第 {page} 页来源页码不一致。")
                else:
                    meta = {'page_number':page,'source_incomplete':True}
            except (OSError, ValueError, RuntimeError) as exc:
                invalid.append(str(exc))
                continue
            self._record_ocr_page(job,page,text,meta)
        if invalid:
            raise ValueError('；'.join(invalid))

    def _run_job(self, job):
        # Imported lazily: the shared PDF capability is also used by the old tools.
        from src.pdf.ocr import PDFOCROptions, build_pdf_book_ocr_image_prompt, ocr_pdf_pages
        options, pages = json.loads(job['options']), json.loads(job['pages'])
        source = self._source_path(job['book_id'])
        checkpoint = self.root / 'ocr' / job['book_id'] / job['id']
        checkpoint.mkdir(parents=True,exist_ok=True)
        self._ingest_saved(job)
        with self._lock, self._db(write=True) as db:
            missing = [p for p in pages if self._page_row(db,job['book_id'],p)['current_revision_id'] is None]
            for p in missing:
                db.execute("UPDATE pages SET ocr_status='running',error='' WHERE book_id=? AND page=?", (job['book_id'],p))
        if self._stop.is_set():
            self._finish_interrupted(job['id'])
            return
        if not missing:
            with self._db(write=True) as db:
                db.execute("UPDATE ocr_jobs SET status='success',errors='{}',error='',updated_at=? WHERE id=?",(now(),job['id']))
            return
        gateway = self._gateways.get(job['id'])
        if gateway is None and self.gateway_factory is not None:
            gateway = self.gateway_factory(options)
        if gateway is None:
            raise ValueError("OCR 连接不可用，请检查服务器设置后继续。")
        plan = PDFOCRRetryPlan.from_dict(json.loads(job['retry_plan'])) if job['retry_plan'] else None
        pdf_options = PDFOCROptions(options['model'], options['reasoning_effort'], options['fast_mode'], options['max_concurrency'], options['submit_interval_seconds'])
        result = ocr_pdf_pages(source,missing,pdf_options,gateway=gateway,checkpoint_dir=checkpoint,
                               page_callback=lambda page,text,meta:self._record_ocr_page(job,page,text,meta),
                               prompt_builder=build_pdf_book_ocr_image_prompt,
                               retry_plan=plan,stop_event=self._stop)
        with self._lock, self._db(write=True) as db:
            errors = {str(page):str(error) for page,error in result.errors_by_page.items()}
            for page,error in result.errors_by_page.items():
                db.execute("UPDATE pages SET ocr_status='failed',error=? WHERE book_id=? AND page=? AND current_revision_id IS NULL", (str(error),job['book_id'],page))
            db.execute("UPDATE pages SET ocr_status='missing' WHERE book_id=? AND current_revision_id IS NULL AND ocr_status='running'",(job['book_id'],))
            remaining = sum(self._page_row(db,job['book_id'],p)['current_revision_id'] is None for p in pages)
            status = 'interrupted' if result.interrupted or self._stop.is_set() else ('partial' if remaining else 'success')
            db.execute("UPDATE ocr_jobs SET status=?,errors=?,error=?,updated_at=? WHERE id=?", (status,json_text(errors),'服务已停止，请手动继续。' if status=='interrupted' else '',now(),job['id']))

    def _finish_interrupted(self, job_id):
        with self._db(write=True) as db:
            db.execute("UPDATE ocr_jobs SET status='interrupted',error='服务已停止，请手动继续。',updated_at=? WHERE id=?",(now(),job_id))
