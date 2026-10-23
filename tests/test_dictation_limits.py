from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import shutil
import wave

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.dictation.cleanup import Cleanup, SessionGatewayClient
from src.dictation.schemas import CleanupRequest, DictationSettings, SessionRequest
from src.dictation.service import DictationService
from tests.test_dictation import completed, dripping_gateway, root
from tests.test_dictation_file_pause import FileWorker, upload


def test_four_hour_pcm_fits_defaults_and_timeouts_are_separate(root):
    from api_server import create_app
    config = DictationSettings()
    assert config.max_upload_minutes == 240
    assert 4 * 3600 * 16000 * 2 + 44 <= config.max_upload_mb * 1024 ** 2
    assert config.max_upload_mb == 512
    assert config.cleanup_timeout_seconds == config.document_cleanup_timeout_seconds == 3600
    assert config.decode_timeout_seconds == 900
    assert DictationSettings(cleanup_timeout_seconds=1).decode_timeout_seconds == 900
    with TestClient(create_app(project_root=root)) as client:
        settings = client.get('/api/dictation/config').json()
        assert settings['max_upload_minutes'] == 240 and settings['max_upload_mb'] == 512
        assert settings['cleanup_timeout_seconds'] == settings['document_cleanup_timeout_seconds'] == 3600
        assert settings['decode_timeout_seconds'] == 900
        assert settings['upload_receive_timeout_seconds'] == 1800


@pytest.mark.parametrize('field,value', [('max_upload_minutes', 241), ('cleanup_timeout_seconds', 3601),
    ('document_cleanup_timeout_seconds', 3601), ('decode_timeout_seconds', 3601), ('decode_timeout_seconds', 0)])
def test_extended_limits_remain_bounded(field, value):
    with pytest.raises(ValidationError):
        DictationSettings(**{field: value})


def test_manual_retry_uses_current_deadlines_without_rewriting_legacy_snapshot(root, monkeypatch):
    seen = []
    def response(client, payload, **kwargs):
        seen.append((payload['reasoning']['effort'], client.timeout_seconds))
        return '{"text":"整理稿","needs_review":false}'
    monkeypatch.setattr(SessionGatewayClient, 'codex_responses_text', response)
    async def run():
        service = DictationService(root)
        state = await completed(service)
        for cfg in (state['worker_config'], state['config']):
            cfg.update(cleanup_timeout_seconds=20, document_cleanup_timeout_seconds=120)
        assert service.cleaner(state).client.timeout_seconds == 20  # Automatic work stays frozen.
        assert service.cleaner(state).document_timeout == 120
        await service.cleanup(state['id'], 'owner', CleanupRequest(scope='utterance', utterance_id='1'))
        await service.cleanup(state['id'], 'owner', CleanupRequest(scope='document'))
        assert seen[0] == ('low', 3600) and seen[1][0] == 'high' and 3599 < seen[1][1] <= 3600
        persisted = service.storage.load(state['id'])
        assert persisted['worker_config']['cleanup_timeout_seconds'] == 20
        assert persisted['worker_config']['document_cleanup_timeout_seconds'] == 120
        assert state['document_suggestion']['metrics']['timeout_seconds'] == 3600
    asyncio.run(run())


def test_default_luna_deadlines_reach_real_curl_for_local_http(dripping_gateway, monkeypatch):
    from src.schemas import CodexLBSettings
    import src.codex_lb_client as transport
    port, *_ = dripping_gateway
    monkeypatch.setenv('DICTATION_LIMIT_TEST_KEY', 'local-test-only')
    gateway = CodexLBSettings(base_url=f'http://127.0.0.1:{port}', base_url_env='',
        api_key_env='DICTATION_LIMIT_TEST_KEY', codex_responses_path='/complete')
    native, deadlines = transport.subprocess.run, []
    def run(command, *args, **kwargs):
        deadlines.append(float(command[command.index('--max-time') + 1]))
        return native(command, *args, **kwargs)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    cleaner = Cleanup(gateway)
    assert cleaner('原稿', [], [])['text'] == '原稿。'
    assert cleaner.document('原稿', [], [])['text'] == '原稿。'
    assert deadlines[0] == 3600 and 3599 < deadlines[1] <= 3600


@pytest.mark.parametrize('decode_timeout,cleanup_timeout,elapsed,expected', [
    (900, 3600, 121, 'completed'), (120, 3600, 121, 'failed'), (900, 1, 121, 'completed')])
def test_file_decode_budget_is_independent_and_can_exceed_legacy_120_seconds(
        root, monkeypatch, decode_timeout, cleanup_timeout, elapsed, expected):
    import src.dictation.service as module
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    path = root / 'config/dictation.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'decode_timeout_seconds': decode_timeout,
        'cleanup_timeout_seconds': cleanup_timeout, 'document_cleanup_timeout_seconds': cleanup_timeout}))
    async def run():
        class DelayedDecode(FileWorker):
            asr_calls = []
            async def send(self, command):
                if self.mode == 'cpu' and command['op'] == 'decode':
                    shutil.copyfile(command['source'], command['destination'])
                    with wave.open(command['source']) as source:
                        samples = source.getnframes()
                    async def deliver():
                        await asyncio.sleep(.02)
                        clock[0] = elapsed  # Simulated worker time, not a 121-second sleep.
                        await self.events.put({'type': 'decoded', 'samples': samples})
                    asyncio.create_task(deliver())
                else:
                    await super().send(command)
        service = DictationService(root, worker_factory=DelayedDecode)
        state, runtime = await upload(service)
        await asyncio.wait_for(runtime.task, 2)
        assert state['status'] == expected
        if expected == 'failed':
            assert '解码超时' in state['error']
        else:
            assert state['samples'] == 3837 and state['audio_total_samples'] == 3837
        assert runtime.lock_file is None
    asyncio.run(run())
