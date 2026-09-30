const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('skills/tapmaker-local-web/scripts/src/tapmaker_local_web/offline_bootstrap.js', 'utf8')
  .replace('__OFFLINE_ORIGIN__', 'https://tapcode-sce.spark.xd.com')
  .replace('__OFFLINE_PREFIX__', '/__tapmaker/offline');
function setup() {
  const requests = [];
  class XHR {open(...args) {requests.push(args);}}
  const window = {fetch(input, init) {requests.push([input, init]); return Promise.resolve(input);}};
  vm.runInNewContext(code, {window, Request, URL, XMLHttpRequest: XHR,
    location: {origin: 'http://127.0.0.1:8875', href: 'http://127.0.0.1:8875/'}});
  return {window, XHR, requests};
}
test('WASM XHR official URLs map only to the finite offline endpoint', () => {
  const {XHR, requests} = setup();
  new XHR().open('GET', 'https://tapcode-sce.spark.xd.com/src/urhox-libs/stable.json?_t=1', true);
  assert.deepEqual(requests[0], ['GET', 'http://127.0.0.1:8875/__tapmaker/offline/src/urhox-libs/stable.json?_t=1', true]);
});
test('fetch Request preserves method and headers', async () => {
  const {window, requests} = setup();
  const r = new Request('https://tapcode-sce.spark.xd.com/src/example', {method: 'POST', headers: {'x-test': 'value'}, body: 'hello'});
  await window.fetch(r);
  assert.equal(requests[0][0].url, 'http://127.0.0.1:8875/__tapmaker/offline/src/example');
  assert.equal(requests[0][0].method, 'POST');
  assert.equal(requests[0][0].headers.get('x-test'), 'value');
  assert.equal(await requests[0][0].text(), 'hello');
});
test('local URLs and unrelated origins are not proxied', async () => {
  const {window, requests} = setup();
  for (const url of ['/assets/local', 'https://tapcode-sce.spark.xd.com.evil/src/a', 'https://other.test/a', 'https://tapcode-sce.spark.xd.com/not-an-asset']) {
    await window.fetch(url);
    assert.equal(requests.at(-1)[0], url);
  }
});
