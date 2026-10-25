"""Eight-session trial defaults and the shared remote request budget."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from fastapi.testclient import TestClient

from src.dictation.cleanup import Cleanup
from src.dictation.schemas import CleanupRequest, DictationSettings, SessionRequest
from src.dictation.service import Conflict, DictationService
from src.schemas import CodexLBSettings
from tests.test_dictation import root, completed


def test_default_eight_sessions_and_ninth_is_rejected(root):
    async def run():
        assert DictationSettings().max_active_sessions == 8
        path = root / 'config/dictation.json'
        config = json.loads(path.read_text())
        config.pop('max_active_sessions')
        path.write_text(json.dumps(config))
        service = DictationService(root)
        try:
            results = await asyncio.gather(*(service.create(str(i), SessionRequest(), start=False) for i in range(9)), return_exceptions=True)
            assert sum(isinstance(r, dict) for r in results) == 8
            assert len(service.recognition.members) == 8
            assert isinstance(results[-1], Conflict) and '容量已满' in str(results[-1])
        finally:
            await service.close()
    asyncio.run(run())


def test_config_reports_effective_preview_and_cleanup_budget(root):
    from api_server import create_app
    with TestClient(create_app(project_root=root)) as client:
        config = client.get('/api/dictation/config').json()
        assert config['max_active_sessions'] == 2  # An existing explicit override is preserved.
        assert config['preview_threads'] == 2
        assert config['cleanup_concurrency'] == 8


def test_eight_manual_cleanups_are_admitted_and_ninth_rejected(root):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = 0
    def factory(*args):
        def clean(text, *args):
            nonlocal calls
            with lock:
                calls += 1
                if calls == 8:
                    entered.set()
            assert release.wait(3)
            return {'text': text, 'needs_review': False}
        return clean
    async def run():
        service = DictationService(root, cleanup_factory=factory)
        states = [await completed(service) for _ in range(9)]
        tasks = [asyncio.create_task(service.cleanup(s['id'], 'owner', CleanupRequest(scope='failed'))) for s in states[:8]]
        try:
            assert await asyncio.to_thread(entered.wait, 1), 'not all eight cleanup requests started'
            with pytest.raises(Conflict, match='队列已满'):
                await service.cleanup(states[8]['id'], 'owner', CleanupRequest(scope='failed'))
            assert calls == 8
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await service.close()
    asyncio.run(run())


def test_global_cleanup_limit_eight_with_actual_loopback_http(root, monkeypatch):
    """The real curl transport runs eight requests together; the ninth waits locally."""
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    stats = {'entered': 0, 'active': 0, 'peak': 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            with guard:
                stats['entered'] += 1
                stats['active'] += 1
                stats['peak'] = max(stats['peak'], stats['active'])
                if stats['entered'] == 8:
                    entered.set()
            try:
                assert release.wait(3)
                payload = {'type':'response.completed', 'response':{'status':'completed', 'model':'local-test',
                    'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'text':'本地样例。','needs_review':False})}]}]}}
                body = ('data: '+json.dumps(payload)+'\n\n').encode()
                self.send_response(200); self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(body))); self.end_headers()
                self.wfile.write(body)
            finally:
                with guard:
                    stats['active'] -= 1
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('DICTATION_CAPACITY_TEST_KEY','local-test-only')
    gateway = CodexLBSettings(base_url=f'http://127.0.0.1:{server.server_port}',base_url_env='',api_key_env='DICTATION_CAPACITY_TEST_KEY')
    async def run():
        service = DictationService(root)
        cleaner = Cleanup(gateway, timeout=3)
        tasks = [asyncio.create_task(service.external(cleaner, '本地样例', [], [])) for _ in range(9)]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.sleep(.03)
            assert stats['entered'] == 8 and stats['peak'] == 8
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(r['text'] == '本地样例。' for r in results)
            assert stats['entered'] == 9 and stats['peak'] == 8
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.close()
    try:
        asyncio.run(run())
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join()
