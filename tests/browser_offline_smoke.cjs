// Node 22+, dedicated Chromium target and a fresh user-data-dir. No TLS overrides.
// PAGE_URL must be an already-running offline DEMO server's actual URL.
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const pageUrl = process.env.PAGE_URL;
  assert.ok(pageUrl, 'Set PAGE_URL to the offline demo URL');
  const origin = new URL(pageUrl).origin;
  assert.equal(new URL(pageUrl).hostname, '127.0.0.1');
  const targets = await (await fetch((process.env.CDP_URL || 'http://127.0.0.1:9359') + '/json/list')).json();
  const target = targets.find(t => t.type === 'page' && t.url === 'about:blank');
  assert.ok(target, 'Use a dedicated blank target in a NEW browser profile');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(resolve => ws.onopen = resolve);
  let sequence = 0;
  const pending = new Map(), external = [], errors = [], responses = [], logs = [], gpuFallback = [];
  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(Error('CDP timeout: ' + method)); }, 120000);
      pending.set(id, {resolve, reject, timer});
      ws.send(JSON.stringify({id, method, params}));
    });
  }
  ws.onmessage = event => {
    const m = JSON.parse(event.data);
    if (m.id) {
      const p = pending.get(m.id); if (!p) return;
      pending.delete(m.id); clearTimeout(p.timer);
      m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result);
    } else if (m.method === 'Fetch.requestPaused') {
      const url = m.params.request.url;
      const allowed = new URL(url).origin === origin;
      if (!allowed) external.push(url);
      send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest',
        {requestId: m.params.requestId, ...(!allowed ? {errorReason: 'BlockedByClient'} : {})}).catch(e => errors.push(String(e)));
    } else if (m.method === 'Network.responseReceived') {
      responses.push({url: m.params.response.url, status: m.params.response.status});
    } else if (m.method === 'Runtime.exceptionThrown') {
      errors.push(m.params.exceptionDetails);
    } else if (m.method === 'Runtime.consoleAPICalled') {
      const values = m.params.args.map(a => a.value || a.description);
      logs.push(values);
      if (process.env.ALLOW_WEBGPU_FALLBACK === '1' && values.length === 1 && values[0] === 'WebGPU bootstrap failed at RequestAdapter: WebGPU not available on this browser (requestAdapter returned null)') gpuFallback.push(values[0]);
      else if (m.params.type === 'error' || values.some(v => typeof v === 'string' && /\bERROR:/.test(v))) errors.push(values);
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error' && !m.params.entry.url?.endsWith('/favicon.ico')) {
      errors.push(m.params.entry);
    }
  };
  async function evaluate(expression) {
    const r = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
    if (r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  try {
    await send('Runtime.enable'); await send('Log.enable'); await send('Network.enable'); await send('Page.enable');
    await send('Network.setCacheDisabled', {cacheDisabled: true});
    await send('Storage.clearDataForOrigin', {origin, storageTypes: 'all'});
    await send('Fetch.enable', {patterns: [{urlPattern: 'http://*'}, {urlPattern: 'https://*'}]});
    await send('Emulation.setDeviceMetricsOverride', {width: 844, height: 390, deviceScaleFactor: 1, mobile: false});
    await send('Page.navigate', {url: pageUrl});
    let ready = false;
    for (let i = 0; i < 90; i++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      ready = await evaluate("!!globalThis.Module?.FS && !!document.getElementById('loading-screen')?.classList.contains('hidden')");
      if (ready || external.length) break;
    }
    assert.equal(external.length, 0, JSON.stringify(external));
    assert.ok(ready, 'Engine failed to start: ' + JSON.stringify(errors.slice(-10)));
    await evaluate('window.tapmakerLocal.ready({timeoutMs:10000})');
    const call = c => evaluate('window.tapmakerLocal.call(' + JSON.stringify(c) + ')');
    assert.deepEqual(await call({type: 'query'}), {started: false, score: 0});
    assert.deepEqual(await call({type: 'start'}), {started: true, score: 0});
    assert.deepEqual(await call({type: 'add_score'}), {started: true, score: 1});
    const shot = await send('Page.captureScreenshot', {format: 'png'});
    if (process.env.SCREENSHOT_PATH) fs.writeFileSync(process.env.SCREENSHOT_PATH, Buffer.from(shot.data, 'base64'));
    assert.deepEqual(await call({type: 'reset'}), {started: false, score: 0});
    assert.equal(external.length, 0, JSON.stringify(external));
    assert.equal(errors.length, 0, JSON.stringify(errors));
    const failed = responses.filter(r => r.status >= 400 && !r.url.endsWith('/favicon.ico'));
    assert.deepEqual(failed, []);
    assert.ok(responses.some(r => r.url.includes('/src/web/src/index.min.js') && r.status === 200));
    assert.ok(responses.some(r => r.url.endsWith('/UrhoXRuntime.wasm') && r.status === 200));
    if (gpuFallback.length) console.log('NOTE: WebGPU unavailable; verified rendered interaction with fallback backend');
    console.log('PASS: fresh cache, offline Player + WASM, Lua query/start/add/reset, zero external requests or errors');
  } catch (error) {
    console.error(error);
    console.error(JSON.stringify({external, errors, failed: responses.filter(r => r.status >= 400)}, null, 2));
    process.exitCode = 1;
  } finally {
    if (process.env.EVIDENCE_PATH) fs.writeFileSync(process.env.EVIDENCE_PATH, JSON.stringify({external, errors, responses, logs, gpuFallback}, null, 2));
    for (const p of pending.values()) clearTimeout(p.timer);
    ws.close();
  }
})();
