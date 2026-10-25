import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { zipSync, strToU8 } from 'fflate';

const root = fileURLToPath(new URL('.', import.meta.url));
export const INSTALL = {
  chrome: `Chrome 开发测试包（Ubuntu / Windows）\n\n1. 将 ZIP 完整解压到一个固定文件夹，不要直接打开 ZIP 内的 manifest。\n2. 在 Chrome 地址栏输入 chrome://extensions 并回车。\n3. 打开右上角“开发者模式”，点击“加载已解压的扩展程序”。\n4. 选择包含 manifest.json 的解压目录；安装后保留该目录。\n5. 从工具栏打开“网页语音输入”，填写提供此下载的服务根地址，点击连接并授权。\n6. 在自动打开的录音页启用扩展控制并试音，切到目标输入框后默认按 Ctrl+Shift+Space 开始 / 结束；快捷键可在 chrome://extensions/shortcuts 查看或改绑。\n7. 本轮结束后，在扩展面板点击“放弃当前操作”，再开始下一轮。\n\n这是 S1 开发测试包，目前只控制录音，尚不回填文字。\n`,
  firefox: `Firefox 临时测试包（Ubuntu / Windows）\n\n1. 将 ZIP 完整解压到一个固定文件夹。\n2. 在 Firefox 地址栏输入 about:debugging#/runtime/this-firefox 并回车。\n3. 点击“临时载入附加组件”，选择解压目录内的 manifest.json。\n4. 从工具栏打开“网页语音输入”，填写提供此下载的服务根地址，点击连接并授权。\n5. 在录音页启用服务端控制，从扩展面板打开 Firefox 采音侧栏，在侧栏授权麦克风并试音。\n6. 保持侧栏可见，回到目标输入框使用侧栏显示的快捷键。\n7. 本轮结束后，在扩展面板点击“放弃当前操作”，再开始下一轮。\n\nFirefox 重启后需重新临时载入。本包没有 Mozilla 正式签名，不用于“从文件安装附加组件”。\n这是 S1 开发测试包，目前只控制录音，尚不回填文字。物理停麦与原生指示器仍待验收。\n`,
};

async function files(directory, prefix = '') {
  const result = {};
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    const name = prefix + entry.name, path = resolve(directory, entry.name);
    if (entry.isDirectory()) Object.assign(result, await files(path, name + '/'));
    else if (entry.isFile()) result[name] = new Uint8Array(await readFile(path));
    else throw new Error(`Unsupported build artifact: ${name}`);
  }
  return result;
}

export async function packageDev(source = resolve(root, 'dist'), destination = resolve(root, '../../frontend/public/dictation-extension')) {
  const catalog = { version: '', kind: 'development', browsers: {} };
  await mkdir(destination, { recursive: true });
  for (const browser of ['chrome', 'firefox']) {
    const directory = resolve(source, browser), manifest = JSON.parse(await readFile(resolve(directory, 'manifest.json'), 'utf8'));
    if (catalog.version && manifest.version !== catalog.version) throw new Error('Browser package versions differ');
    catalog.version = manifest.version;
    const minimumVersion = browser === 'chrome' ? manifest.minimum_chrome_version : manifest.browser_specific_settings.gecko.strict_min_version;
    const entries = await files(directory);
    for (const required of ['manifest.json', 'background.js', 'recorder.js', 'popup.html', 'options.html',
      ...(browser === 'firefox' ? ['sidebar.html', 'dictation-worklet.js'] : [])]) {
      if (!entries[required]) throw new Error(`Missing ${browser} resource: ${required}`);
    }
    entries['INSTALL.txt'] = strToU8(INSTALL[browser]);
    const filename = `dictation-${browser}-dev.zip`, data = zipSync(entries, { level: 6 });
    await writeFile(resolve(destination, filename), data);
    catalog.browsers[browser] = { filename, minimumVersion, bytes: data.byteLength };
  }
  await writeFile(resolve(destination, 'packages.json'), JSON.stringify(catalog, null, 2) + '\n');
  return catalog;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) console.log(await packageDev());
