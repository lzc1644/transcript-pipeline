import { build } from 'vite';
import { mkdir, readFile, writeFile, copyFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const browser = process.argv[2];
if (!['chrome', 'firefox'].includes(browser)) throw new Error('Choose chrome or firefox');
const outDir = resolve(root, 'dist', browser);
await build({ root, configFile: false, base: './', build: { outDir, emptyOutDir: true,
  rollupOptions: { input: { popup: resolve(root, 'popup.html'), options: resolve(root, 'options.html'), ...(browser === 'firefox' ? { sidebar: resolve(root, 'sidebar.html') } : {}) } } } });
for (const [entry, name] of [['background', 'background'], ['content/recorder', 'recorder']]) {
  await build({ root, configFile: false, build: { outDir, emptyOutDir: false, minify: false,
    lib: { entry: resolve(root, `src/${entry}.ts`), name: `Dictation_${name}`, formats: ['iife'], fileName: () => `${name}.js` } } });
}
const common = JSON.parse(await readFile(resolve(root, 'manifests/common.json')));
const specific = JSON.parse(await readFile(resolve(root, `manifests/${browser}.json`)));
await mkdir(outDir, { recursive: true });
await writeFile(resolve(outDir, 'manifest.json'), JSON.stringify({ ...common, ...specific }, null, 2) + '\n');
if (browser === 'firefox') await copyFile(resolve(root, '../../frontend/public/dictation-worklet.js'), resolve(outDir, 'dictation-worklet.js'));
