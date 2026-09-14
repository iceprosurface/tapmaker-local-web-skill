const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
 const cdp=process.env.CDP_URL || 'http://127.0.0.1:9359';
 const page=process.env.PAGE_URL || 'http://127.0.0.1:8875/';
 const targets=await (await fetch(cdp+'/json/list')).json();
 const target=targets.find(t=>t.type==='page' && t.url.startsWith(page));
 assert.ok(target, 'Open the isolated demo once before running this test');
 const ws=new WebSocket(target.webSocketDebuggerUrl); await new Promise(r=>ws.onopen=r);
 let seq=0;const pending=new Map(), logs=[];
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);}else if(m.method==='Runtime.consoleAPICalled'||m.method==='Runtime.exceptionThrown'||m.method==='Log.entryAdded'){logs.push(m);}};
 function send(method,params={}){return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});}
 async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
 try{
 await send('Runtime.enable');await send('Log.enable');await send('Page.enable');
 await send('Emulation.setDeviceMetricsOverride',{width:844,height:390,deviceScaleFactor:1,mobile:false});
 // 复用既有 target，不打开、导航或聚焦页面。
 for(let i=0;i<24;i++){
  await new Promise(r=>setTimeout(r,5000));
  const status=await evaluate(`({title:document.title,loading:document.getElementById('loading-screen')?.className,bridge:typeof window.tapmakerLocal,fs:!!globalThis.Module?.FS,error:document.getElementById('dialog-overlay')?.innerText})`);
  console.log('startup',JSON.stringify(status));
  if(status.bridge==='object'&&status.fs&&status.loading?.includes('hidden'))break;
 }
 console.log('registration',await evaluate('window.tapmakerLocal.ready({timeoutMs:10000})'));
 const call=c=>evaluate('window.tapmakerLocal.call('+JSON.stringify(c)+')');
 await call({type:'reset'});
 assert.deepEqual(await call({type:'query'}),{started:false,score:0});
 assert.deepEqual(await call({type:'start'}),{started:true,score:0});
 const t=Date.now();
 const results=await evaluate(`Promise.all(Array.from({length:20},()=>window.tapmakerLocal.call({type:'add_score'})))`);
 assert.deepEqual(results.map(r=>r.score),Array.from({length:20},(_,i)=>i+1));
 console.log('20 sequential actions ms',Date.now()-t);
 const rejected=await evaluate(`window.tapmakerLocal.call({type:'eval'}).then(()=>false,e=>e.message)`);
 assert.equal(rejected,'unsupported_command');
 assert.deepEqual(await call({type:'query'}),{started:true,score:20});
 if(process.env.SCREENSHOT_PATH){const shot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(process.env.SCREENSHOT_PATH,Buffer.from(shot.data,'base64'));}
 assert.deepEqual(await call({type:'reset'}),{started:false,score:0});
 console.log('PASS: query, start, 20 concurrent calls serialized, unknown command rejection, reset');
 const errors=logs.filter(x=>x.method==='Runtime.exceptionThrown'||x.params?.type==='error'||(x.params?.entry?.level==='error'&&!x.params.entry.url?.endsWith('/favicon.ico')));
 assert.equal(errors.length,0,JSON.stringify(errors));
 console.log('PASS: no browser or runtime errors during commands');
 }catch(e){console.error(e);console.error('recent logs',JSON.stringify(logs.slice(-15).map(m=>m.params.args?.map(a=>a.value))));process.exitCode=1;}finally{ws.close();}
})();
