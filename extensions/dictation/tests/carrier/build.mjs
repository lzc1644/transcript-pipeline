import {build} from 'vite';import{writeFile,copyFile}from'node:fs/promises';import{resolve}from'node:path';
const root=import.meta.dirname, browser=process.argv[2]??'firefox', carrier=process.argv[3]??'tab', outDir=resolve(root,'../../dist/carrier-'+browser+'-'+carrier);
await build({root,configFile:false,base:'./',build:{outDir,emptyOutDir:true,rollupOptions:{input:resolve(root,'recorder.html')}}});
await build({root,configFile:false,build:{outDir,emptyOutDir:false,minify:false,lib:{entry:resolve(root,'background.ts'),name:'CarrierProbe',formats:['iife'],fileName:()=> 'background.js'}}});
await copyFile(resolve(root,'../../../../frontend/public/dictation-worklet.js'),resolve(outDir,'dictation-worklet.js'));
const manifest={manifest_version:3,name:'录音承载穿刺（无网络）',version:'0.1.0',permissions:['storage','activeTab','scripting'],incognito:'not_allowed',commands:{'toggle-dictation':{suggested_key:{default:'Ctrl+Shift+Space'},description:'录音承载穿刺'}}};
if(browser==='firefox'){manifest.background={scripts:['background.js'],persistent:false};manifest.browser_specific_settings={gecko:{id:`dictation-carrier-${carrier}@transcript-pipeline.local`,strict_min_version:'154.0'}};if(carrier==='sidebar')manifest.sidebar_action={default_panel:'recorder.html',default_title:'语音输入录音穿刺',open_at_install:false};}else{manifest.background={service_worker:'background.js'};manifest.minimum_chrome_version='155';}
await writeFile(resolve(outDir,'observer.html'),'<!doctype html><html><head><meta charset="UTF-8"><title>录音穿刺观察</title></head><body><h1>录音穿刺观察</h1><p>仅查看当前扩展的采音状态。</p></body></html>');
await writeFile(resolve(outDir,'manifest.json'),JSON.stringify(manifest,null,2));
