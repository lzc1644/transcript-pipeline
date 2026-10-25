import test from 'node:test';
import assert from 'node:assert/strict';
import { message, envelope, serviceOrigin, hostPermission, servicePage } from '../../../shared/dictationExtensionProtocol.ts';

test('service configuration preserves scheme/host/port and restricts development HTTP', () => {
  assert.equal(serviceOrigin('https://192.168.0.252:8443/dictation'), 'https://192.168.0.252:8443');
  for (const value of ['http://localhost:8031', 'http://127.0.0.1:8031/', 'http://[::1]:8031/dictation/extension']) assert.equal(serviceOrigin(value), new URL(value).origin);
  for (const value of ['http://192.168.0.252:8080', 'http://other.local', 'file:///tmp', 'javascript:alert(1)',
    'https://user:secret@server', 'https://server/?start=1', 'https://server/#start', 'https://server/prefix/dictation'])
    assert.throws(() => serviceOrigin(value), undefined, value);
  assert.equal(hostPermission('https://192.168.0.252:8443'), 'https://192.168.0.252/*');
  assert.equal(hostPermission('http://[::1]:8031'), 'http://[::1]/*');
});
test('port and service path are checked at runtime, not inferred from a host permission', () => {
  const origin = 'https://192.168.0.252:8443';
  assert(servicePage(`${origin}/dictation?mode=live`, origin));
  assert(servicePage(`${origin}/dictation/extension`, origin, true));
  for (const value of ['https://192.168.0.252/dictation', 'https://192.168.0.252:8444/dictation',
    `${origin}/dictation/extension/anything`, `${origin}/dictation-evil`, 'https://evil.example/dictation', undefined])
    assert(!servicePage(value, origin), String(value));
  assert(!servicePage(`${origin}/dictation`, origin, true));
});
test('finite, versioned envelopes reject malformed controls and future delivery messages', () => {
  const valid = message('START', { operationId: 'operation-1', documentInstanceId: 'document-1' });
  assert(envelope(valid));
  for (const invalid of [null, [], 'START', { ...valid, type: 'COMMIT' }, { ...valid, type: 'RESULT' },
    { ...valid, protocolVersion: 2 }, { ...valid, channel: 'wrong' }, { ...valid, requestId: '' },
    { ...valid, sessionId: {} }, { ...valid, operationId: '<script>' }]) assert(!envelope(invalid));
  assert.notEqual(message('HELLO').requestId, message('HELLO').requestId);
});
