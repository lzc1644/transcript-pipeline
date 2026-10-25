import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, posix } from 'node:path';
import { test } from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { unzipSync, strFromU8 } from 'fflate';
import { packageDev, INSTALL } from '../package.mjs';

const extension = resolve(import.meta.dirname, '..');

test('real browser builds produce installable development ZIPs and a matching catalog', async () => {
  const output = await mkdtemp(resolve(tmpdir(), 'dictation-packages-'));
  try {
    for (const browser of ['chrome', 'firefox']) {
      await promisify(execFile)(process.execPath, [resolve(extension, 'build.mjs'), browser], { cwd: extension });
    }
    const catalog = await packageDev(undefined, output);
    const pkg = JSON.parse(await readFile(resolve(extension, 'package.json'), 'utf8'));
    assert.equal(catalog.version, pkg.version);
    assert.equal(catalog.kind, 'development');
    assert.deepEqual(JSON.parse(await readFile(resolve(output, 'packages.json'), 'utf8')), catalog);
    for (const browser of ['chrome', 'firefox']) {
      const entry = catalog.browsers[browser];
      assert.equal(entry.filename, `dictation-${browser}-dev.zip`);
      const zip = await readFile(resolve(output, entry.filename));
      assert.equal(zip.byteLength, entry.bytes);
      const files = unzipSync(zip), names = Object.keys(files);
      assert(names.includes('manifest.json'), 'manifest must be at the ZIP root');
      assert.equal(strFromU8(files['INSTALL.txt']), INSTALL[browser]);
      assert(names.every(name => /^(manifest\.json|background\.js|recorder\.js|popup\.html|options\.html|sidebar\.html|dictation-worklet\.js|INSTALL\.txt|assets\/[^/]+\.(js|css))$/.test(name)),
        `unexpected build files: ${names}`);
      const manifest = JSON.parse(strFromU8(files['manifest.json']));
      assert.equal(manifest.version, catalog.version);
      assert.equal(entry.minimumVersion, browser === 'chrome' ? manifest.minimum_chrome_version : manifest.browser_specific_settings.gecko.strict_min_version);
      for (const path of [manifest.action.default_popup, manifest.options_ui.page,
        ...(browser === 'chrome' ? [manifest.background.service_worker] : [...manifest.background.scripts, manifest.sidebar_action.default_panel]), 'recorder.js']) {
        assert(files[path], `missing manifest/injected resource: ${path}`);
      }
      for (const name of names.filter(name => name.endsWith('.html'))) {
        for (const [, resource] of strFromU8(files[name]).matchAll(/(?:src|href)="([^"#]+)"/g)) {
          assert(!/^(?:[a-z]+:|\/\/)/i.test(resource), `remote HTML resource: ${resource}`);
          assert(files[posix.normalize(posix.join(posix.dirname(name), resource))], `broken ${name} reference: ${resource}`);
        }
      }
      assert.match(INSTALL[browser], /Ubuntu \/ Windows/);
      assert.match(INSTALL[browser], /尚不回填/);
      if (browser === 'firefox') {
        assert(files['dictation-worklet.js']);
        assert.match(INSTALL[browser], /重启后需重新临时载入/);
        assert.match(INSTALL[browser], /保持侧栏可见/);
      }
    }
  } finally { await rm(output, { recursive: true, force: true }); }
});

async function invalidBuild(t, change) {
  const root = await mkdtemp(resolve(tmpdir(), 'dictation-invalid-package-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const browser of ['chrome', 'firefox']) {
    const dir = resolve(root, 'dist', browser);
    await mkdir(dir, { recursive: true });
    const manifest = { version: '1.2.3', minimum_chrome_version: '155', browser_specific_settings: { gecko: { strict_min_version: '154.0' } } };
    for (const name of ['background.js', 'recorder.js', 'popup.html', 'options.html', ...(browser === 'firefox' ? ['sidebar.html', 'dictation-worklet.js'] : [])]) {
      await writeFile(resolve(dir, name), 'test');
    }
    await writeFile(resolve(dir, 'manifest.json'), JSON.stringify(manifest));
  }
  await change(root);
  return packageDev(resolve(root, 'dist'), resolve(root, 'out'));
}

test('missing required browser resource fails packaging', async t => {
  await assert.rejects(invalidBuild(t, root => rm(resolve(root, 'dist/firefox/dictation-worklet.js'))), /Missing firefox resource/);
});

test('different browser versions cannot be published as one catalog', async t => {
  await assert.rejects(invalidBuild(t, async root => {
    const path = resolve(root, 'dist/firefox/manifest.json'), manifest = JSON.parse(await readFile(path));
    manifest.version = '1.2.4'; await writeFile(path, JSON.stringify(manifest));
  }), /versions differ/);
});

test('symlinks cannot pull files outside the build into a package', async t => {
  await assert.rejects(invalidBuild(t, async root => {
    await writeFile(resolve(root, 'private.txt'), 'not a build artifact');
    await symlink(resolve(root, 'private.txt'), resolve(root, 'dist/chrome/private.txt'));
  }), /Unsupported build artifact/);
});
