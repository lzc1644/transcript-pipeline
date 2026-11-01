from __future__ import annotations

import hashlib
import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from pypdf import PdfReader

from src.pdf.workflow import build_pdf_ocr_run_identity
from .store import LibraryError, new_id


TASK_ID = re.compile(r'pdf-ocr-[0-9a-f]{32}')
CANDIDATE_ID = re.compile(r'(pdf-ocr-[0-9a-f]{32}):([0-9]+)')


class ImportsMixin:
    @contextmanager
    def history_guard(self, task_id):
        with self._lock:
            yield

    def active_import_task_ids(self):
        with self._lock:
            return set(self._active_import_tasks)

    def _history_source(self, raw, label):
        root = (self.project_root / 'data/uploads/pdf-ocr').resolve()
        if not isinstance(raw,str) or not raw:
            return None
        if raw.startswith('/app/data/'):
            path = self.project_root / 'data' / raw[len('/app/data/'):]
        else:
            path = Path(raw)
        path = path.resolve()
        if not path.is_relative_to(root):
            return None
        if path.is_file():
            if path.name != label or path.suffix.lower() != '.pdf':
                return None
            return path
        if path.is_dir():
            candidate = (path / label).resolve()
            if candidate.is_relative_to(path) and candidate.is_relative_to(root) and candidate.is_file() and candidate.suffix.lower() == '.pdf':
                return candidate
        return None

    def _history_candidate(self, task_dir, index, item, state):
        label = item.get('source_file')
        if not isinstance(label,str) or not label:
            return None
        payload = state.get('request_payload') or {}
        summary = state.get('input_summary') or {}
        raw_input = payload.get('input_path') or summary.get('input_path')
        source = self._history_source(raw_input,label)
        warnings = []
        page_count = item.get('page_count') or 0
        metadata = {'id':f'{task_dir.name}:{index}', 'task_id':task_dir.name,
                    'source_file':label, 'title':Path(label).stem, 'page_count':page_count,
                    'reusable_pages':0, 'warnings':warnings, '_pdf':source, '_pages':{}, '_digest':None}
        if source is None:
            warnings.append('原 PDF 无法在受限上传目录中确认。')
            return metadata
        try:
            actual_count = len(PdfReader(source).pages)
        except Exception:
            warnings.append('原 PDF 无法读取；无法导入。')
            metadata['_pdf'] = None
            return metadata
        metadata['page_count'] = actual_count
        if page_count and page_count != actual_count:
            warnings.append('历史页数与原 PDF 不一致；仅可导入 PDF。')
            return metadata
        page_count = actual_count
        model = payload.get('ocr_model')
        effort = payload.get('ocr_reasoning_effort') or ''
        if not isinstance(model,str) or not model.strip():
            warnings.append('缺少 OCR 主模型，无法验证页面身份；仅可导入 PDF。')
            return metadata
        identity = build_pdf_ocr_run_identity(source,model,effort)
        metadata['_digest'] = identity.source_digest
        recorded = item.get('ocr_identity')
        if recorded and recorded != identity.fingerprint:
            warnings.append('历史 OCR 身份与原 PDF 或设置不一致；仅可导入 PDF。')
            return metadata
        # Path layout matches PDF book OCR: pages/<relative parent>/<stem>.pages/<identity>.
        relative = Path(label)
        base = (task_dir / 'pages' / relative.parent / f'{relative.stem}.pages').resolve()
        if not base.is_relative_to((task_dir / 'pages').resolve()):
            warnings.append('历史页路径无效；仅可导入 PDF。')
            return metadata
        checkpoint = base / identity.fingerprint
        if checkpoint.is_symlink() or not checkpoint.is_dir():
            warnings.append('没有可验证的逐页检查点；仅可导入 PDF。')
            return metadata
        missing_source = False
        invalid = 0
        for file in checkpoint.glob('page-*.txt'):
            match = re.fullmatch(r'page-([0-9]{6})\.txt',file.name)
            if not match:
                continue
            page = int(match.group(1))
            if not 1 <= page <= page_count:
                invalid += 1
                continue
            try:
                text = file.read_text(encoding='utf-8')
                sidecar = checkpoint / f'page-{page:06d}.source.json'
                if sidecar.is_file():
                    data = json.loads(sidecar.read_text(encoding='utf-8'))
                    if (not isinstance(data,dict) or data.get('page_number') != page
                            or data.get('primary_model') != model.strip()
                            or not isinstance(data.get('model'),str) or not data['model'].strip()
                            or data.get('text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()):
                        invalid += 1
                        continue
                else:
                    data = {'page_number':page,'source_incomplete':True}
                    missing_source = True
                metadata['_pages'][page] = {'text':text,'source':data,'file':file,
                                            'sidecar':sidecar if sidecar.is_file() else None}
            except (OSError,UnicodeError,ValueError):
                invalid += 1
        if missing_source or not recorded:
            warnings.append('部分历史页来源信息不完整；按已验证的父身份保留。')
        if invalid:
            warnings.append(f'{invalid} 页来源或页码无法校验，未复用。')
        metadata['reusable_pages'] = len(metadata['_pages'])
        return metadata

    def _scan_history(self, task_ids=None):
        root = self.project_root / 'data/jobs/pdf-ocr'
        result = []
        if not root.is_dir():
            return result
        for task_dir in sorted(root.iterdir()):
            if not TASK_ID.fullmatch(task_dir.name) or task_dir.is_symlink() or not task_dir.is_dir():
                continue
            if task_ids is not None and task_dir.name not in task_ids:
                continue
            try:
                state = json.loads((task_dir/'state.json').read_text(encoding='utf-8'))
                for index,item in enumerate(state.get('items') or []):
                    if not isinstance(item,dict):
                        continue
                    candidate = self._history_candidate(task_dir,index,item,state)
                    if candidate:
                        result.append(candidate)
            except (OSError,ValueError,TypeError):
                continue
        return result

    def history_candidates(self):
        items = self._scan_history()
        texts = {}
        for item in items:
            if item['_digest']:
                for page,result in item['_pages'].items():
                    texts.setdefault((item['_digest'],page),set()).add(result['text'])
        public = []
        for item in items:
            entry = {k:v for k,v in item.items() if not k.startswith('_')}
            entry['conflicting_pages'] = [page for page in item['_pages']
                if len(texts.get((item['_digest'],page),())) > 1]
            public.append(entry)
        return {'items':public,'total':len(public)}

    def import_history(self, candidate_ids):
        if not isinstance(candidate_ids,list) or not candidate_ids or any(not isinstance(cid,str) or not CANDIDATE_ID.fullmatch(cid) for cid in candidate_ids):
            raise LibraryError(400,'请选择有效的历史候选。')
        selected_ids = list(dict.fromkeys(candidate_ids))
        task_ids = {CANDIDATE_ID.fullmatch(cid).group(1) for cid in selected_ids}
        with self._lock:
            if self._active_import_tasks.intersection(task_ids) or any(self.history_task_active(task_id) for task_id in task_ids):
                raise LibraryError(409,'所选历史任务正在执行或导入。')
            with self._db(write=True) as db:
                operation_id = self._create_operation(db,'import',len(selected_ids),{'candidate_ids':selected_ids})
            self._active_import_tasks.update(task_ids)
        try:
            candidates = {c['id']:c for c in self._scan_history(task_ids)}
            candidates = {cid:candidates[cid] for cid in selected_ids if cid in candidates}
            collisions = {}
            for cid,candidate in candidates.items():
                digest = candidate['_digest']
                if digest:
                    for page,result in candidate['_pages'].items():
                        collisions.setdefault((digest,page),{}).setdefault(result['text'],[]).append(cid)
            conflicts = {key for key,values in collisions.items() if len(values)>1}
            results = []
            for cid in selected_ids:
                c = candidates.get(cid)
                if not c or c['_pdf'] is None:
                    results.append({'id':cid,'status':'failed','book_id':None,'imported_pages':0,
                                    'warnings':c['warnings'] if c else [],'error':'历史候选或原 PDF 不存在。'})
                    self._update_operation(operation_id,completed=len(results),report=results)
                    continue
                book_id = None
                reserved = False
                warnings = list(c['warnings'])
                try:
                    book = self.add_book(c['_pdf'],filename=Path(c['source_file']).name,title=c['title'])
                    book_id = book['id']
                    with self._lock, self._db() as db:
                        row = self._book_row(db,book_id)
                        if c['_digest'] and row['digest'] != c['_digest']:
                            raise LibraryError(409,'历史 PDF 在读取期间发生变化，请重新扫描。')
                        self._assert_book_idle(db,book_id)
                        self._busy_books[book_id] = self._busy_books.get(book_id,0)+1
                        self._importing_books.add(book_id)
                        reserved = True
                    accepted = {page:data for page,data in c['_pages'].items() if (c['_digest'],page) not in conflicts}
                    if len(accepted) != len(c['_pages']):
                        warnings.append('同一 PDF 的所选候选存在页文本冲突；冲突页未导入，请单独选择来源。')
                    # Preserve the source checkpoints independently of the legacy task.
                    dest = self.root / 'ocr' / book_id / ('history-' + cid.replace(':','-'))
                    if accepted:
                        stage = self.root / 'staging' / new_id()
                        stage.mkdir()
                        try:
                            for data in accepted.values():
                                shutil.copy2(data['file'],stage/data['file'].name)
                                if data['sidecar']:
                                    shutil.copy2(data['sidecar'],stage/data['sidecar'].name)
                            dest.mkdir(parents=True,exist_ok=True)
                            for staged in stage.iterdir():
                                target = dest / staged.name
                                if not target.exists():
                                    staged.rename(target)
                        finally:
                            shutil.rmtree(stage,ignore_errors=True)
                    imported = 0
                    manual = 0
                    with self._lock, self._db(write=True) as db:
                        for page,data in accepted.items():
                            current = self._page_row(db,book_id,page)
                            if db.execute('SELECT 1 FROM history_page_imports WHERE candidate_id=? AND book_id=? AND page=?',(cid,book_id,page)).fetchone():
                                continue
                            if current['current_revision_id']:
                                head = db.execute('SELECT source_kind FROM page_revisions WHERE id=?',(current['current_revision_id'],)).fetchone()
                                if head['source_kind'] == 'manual':
                                    manual += 1
                                    continue
                            rev = self._append_revision(db,book_id,page,data['text'],'import',
                                {'candidate_id':cid,'task_id':c['task_id'],**data['source']},current['current_revision_id'])
                            db.execute('INSERT INTO history_page_imports VALUES(?,?,?,?)',(cid,book_id,page,rev))
                            imported += 1
                    if manual:
                        warnings.append(f'{manual} 页已有人工修订，未覆盖。')
                    outcome = 'partial' if len(accepted) != len(c['_pages']) else 'success'
                    results.append({'id':cid,'status':outcome,'book_id':book_id,'imported_pages':imported,'warnings':warnings,'error':''})
                except (LibraryError,OSError,ValueError) as exc:
                    results.append({'id':cid,'status':'failed','book_id':book_id,'imported_pages':0,'warnings':warnings,'error':str(exc)})
                finally:
                    if reserved:
                        with self._lock:
                            self._importing_books.discard(book_id)
                            self._busy_books[book_id] -= 1
                            if not self._busy_books[book_id]:
                                del self._busy_books[book_id]
                self._update_operation(operation_id,completed=len(results),report=results)
            statuses = [item['status'] for item in results]
            status = 'success' if all(value == 'success' for value in statuses) else ('failed' if all(value == 'failed' for value in statuses) else 'partial')
            self._update_operation(operation_id,status=status,completed=len(results),report=results)
            return {'items':results,'total':len(results),'job_id':operation_id}
        except Exception as exc:
            self._update_operation(operation_id,status='failed',error=str(exc))
            raise
        finally:
            with self._lock:
                self._active_import_tasks.difference_update(task_ids)
