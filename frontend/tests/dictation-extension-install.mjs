// Exercises real page links and ZIPs. Local fixture by default; optional authorized
// LAN origin is read-only: every non-GET/HEAD API request is blocked and fails the test.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, open, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
const { unzipSync, strFromU8 } = createRequire(new URL('../../extensions/dictation/package.json', import.meta.url))('fflate');
const root = resolve(import.meta.dirname, '../..');
const output = resolve(root, process.env.DICTATION_INSTALL_OUTPUT || `tmp/browser-dictation-extension/install-${Date.now()}`);
const base = process.env.DICTATION_INSTALL_URL || 'http://127.0.0.1:8033';
const local = !process.env.DICTATION_INSTALL_URL;
await mkdir(output, { recursive: false });
let browser, page, server, log, outcome = 'failed';
const checks = [], errors = [], mutations = [], downloads = [];
const delay = ms => new Promise(r => setTimeout(r, ms));
async function checkLayout() {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow');
  const [controls, install, paper] = await Promise.all([
    page.locator('.extension-actions').first().boundingBox(),
    page.locator('#extension-install').boundingBox(), page.locator('.extension-paper').boundingBox(),
  ]);
  assert(controls.y + controls.height <= install.y, 'installation guide covers stop controls');
  assert(install.y + install.height <= paper.y, 'expanded instructions cover transcript');
}
try {
  if (local) {
    log = await open(`${output}/server.log`, 'w');
    server = spawn(`${root}/.venv/bin/python`, ['extensions/dictation/tests/serve_fixture.py', '--root', `${output}/fixture`, '--port', '8033'],
      { cwd: root, stdio: ['ignore', log.fd, log.fd] });
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { const value = await fetch(base + '/api/health').then(r => r.json()); if (value.fixture) { ready = true; break; } } catch {}
      if (server.exitCode !== null) throw Error('isolated fixture exited; check server.log');
      await delay(100);
    }
    assert(ready, 'isolated fixture did not become ready');
  }
  browser = await chromium.launch({ headless: true, ...(process.env.CHROME_TEST_EXECUTABLE ? { executablePath: process.env.CHROME_TEST_EXECUTABLE } : {}),
    env: { ...process.env, ...(process.env.DICTATION_INSTALL_BROWSER_HOME ? { HOME: process.env.DICTATION_INSTALL_BROWSER_HOME } : {}) } });
  const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, permissions: ['clipboard-read', 'clipboard-write'] });
  await context.route('**/api/**', route => {
    if (!['GET', 'HEAD'].includes(route.request().method())) { mutations.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await context.addInitScript(() => {
    window.__installMicCalls = 0;
    if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = async () => { window.__installMicCalls++; throw Error('Installation test must not capture'); };
  });
  page = await context.newPage(); page.setDefaultTimeout(10000); page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + '/dictation');
  await page.getByRole('button', { name: '扩展下载与安装', exact: true }).click();
  await page.waitForURL(base + '/dictation/extension#extension-install');
  const section = page.getByRole('region', { name: '扩展下载与安装', exact: true });
  await section.getByRole('link', { name: '下载 Chrome ZIP', exact: true }).waitFor();
  assert(await page.evaluate(() => isSecureContext));
  await checkLayout();
  checks.push('ordinary dictation page leads to both actual download links without starting capture');
  await page.screenshot({ path: `${output}/desktop-collapsed.png`, fullPage: true });
  const catalog = await context.request.get(base + '/dictation-extension/packages.json').then(r => { assert(r.ok()); return r.json(); });
  for (const name of ['chrome', 'firefox']) {
    const label = name === 'chrome' ? 'Chrome' : 'Firefox';
    const pending = page.waitForEvent('download'); await section.getByRole('link', { name: `下载 ${label} ZIP`, exact: true }).click();
    const download = await pending, entry = catalog.browsers[name];
    assert.equal(download.suggestedFilename(), entry.filename); assert.equal(await download.failure(), null);
    const path = `${output}/${entry.filename}`; await download.saveAs(path);
    const data = await readFile(path), files = unzipSync(data);
    const manifest = JSON.parse(strFromU8(files['manifest.json']));
    assert.equal(manifest.version, catalog.version); assert.equal(data.byteLength, entry.bytes);
    assert(files['background.js'] && files['recorder.js'] && files['INSTALL.txt']);
    assert.match(strFromU8(files['INSTALL.txt']), /Ubuntu \/ Windows/);
    if (name === 'firefox') assert(files['sidebar.html'] && files['dictation-worklet.js']);
    downloads.push({ browser: name, filename: entry.filename, version: manifest.version, bytes: data.byteLength, entries: Object.keys(files) });
  }
  checks.push('both browser clicks download complete ZIPs, with root manifests, browser resources and INSTALL.txt');
  await section.locator('summary').click();
  await section.getByRole('button', { name: 'Chrome', exact: true }).click();
  await section.getByText('chrome://extensions', { exact: true }).waitFor();
  assert.match(await section.innerText(), /保留这个目录/);
  await section.getByRole('button', { name: 'Firefox', exact: true }).focus(); await page.keyboard.press('Enter');
  await section.getByText('about:debugging#/runtime/this-firefox', { exact: true }).waitFor();
  assert.match(await section.innerText(), /重启后需重新载入/);
  assert.match(await section.innerText(), /保持侧栏可见/);
  await section.getByRole('button', { name: '复制服务地址', exact: true }).click();
  await section.getByRole('button', { name: '已复制', exact: true }).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), base);
  await checkLayout();
  await page.screenshot({ path: `${output}/desktop-expanded.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 }); await checkLayout();
  await page.screenshot({ path: `${output}/mobile-expanded.png`, fullPage: true });
  checks.push('browser guides, keyboard selection, restart warning and copied current origin work; expanded layout fits desktop and 390px');
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw Error('clipboard denied'); }; });
  await section.getByRole('button', { name: '已复制', exact: true }).click();
  await section.getByText('复制未成功，请选中并复制上方服务地址。', { exact: true }).waitFor();
  assert.equal(await section.getByRole('link').count(), 2);
  checks.push('clipboard rejection leaves address and both downloads usable');
  await page.route('**/dictation-extension/packages.json', route => route.fulfill({ status: 404, body: '' }));
  await page.reload(); await section.getByRole('button', { name: '重试', exact: true }).waitFor();
  assert.equal(await section.getByRole('link').count(), 0);
  await page.screenshot({ path: `${output}/missing-package.png`, fullPage: true });
  await page.unroute('**/dictation-extension/packages.json');
  await section.getByRole('button', { name: '重试', exact: true }).click();
  await section.getByRole('link', { name: '下载 Firefox ZIP', exact: true }).waitFor();
  await page.route('**/dictation-extension/packages.json', route => route.fulfill({ json: { ...catalog, browsers: { ...catalog.browsers, chrome: { filename: 'https://invalid.example/package.zip', minimumVersion: '155' } } } }));
  await page.reload(); await section.getByRole('button', { name: '重试', exact: true }).waitFor();
  assert.equal(await section.getByRole('link').count(), 0);
  checks.push('missing or malformed catalog removes download links; retry restores valid packages');
  assert.equal(await page.evaluate(() => window.__installMicCalls), 0);
  assert.deepEqual(mutations, []); assert.deepEqual(errors, []);
  outcome = 'passed';
} finally {
  if (outcome !== 'passed' && page) {
    await writeFile(`${output}/page.txt`, await page.locator('body').innerText().catch(() => 'unavailable'));
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  }
  await writeFile(`${output}/report.json`, JSON.stringify({ outcome, origin: base, fixture: local, checks, downloads, errors, mutations, paidCalls: 0 }, null, 2) + '\n');
  await browser?.close();
  if (server && server.exitCode === null) { server.kill('SIGTERM'); await new Promise(r => server.once('exit', r)); }
  await log?.close(); console.log(output, outcome, checks);
}
