const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../skills/tapmaker-local-web/scripts/src/tapmaker_local_web/local_command.js'), 'utf8');
function setup({loading=false, respond=true}={}) {
  let reads=0, writes=[], response={}, id=0;
  const context={location:{host:'localhost:8875'},navigator:{}, console, TextEncoder, Date, setTimeout,
    crypto:{randomUUID:()=>String(++id)},
    document:{getElementById:()=>({classList:{contains:()=>!loading}})},
    Module:{FS:{readFile(name){ reads++; return JSON.stringify(name.includes('registration')?{query:true}:response); },
      writeFile(name,raw){const request=JSON.parse(raw); writes.push(request); if(respond)response={id:request.id,result:request.command.value??42};}}}};
  context.window=context; vm.runInNewContext(source,context);
  return {api:context.tapmakerLocal,writes,reads:()=>reads,ack:()=>response={id:writes.at(-1).id,result:42}};
}
test('loading guard does not touch FS',async()=>{const s=setup({loading:true});await assert.rejects(s.api.ready({timeoutMs:5}),/not_ready/);assert.equal(s.reads(),0);});
test('whitelist and request limit reject before write',async()=>{const s=setup();await assert.rejects(s.api.call({type:'eval'}),/unsupported/);await assert.rejects(s.api.call({type:'query',value:'水'.repeat(2000)}),/too_large/);assert.equal(s.writes.length,0);});
test('queued calls snapshot input and correlate responses',async()=>{const s=setup();const command={type:'query',value:1};const a=s.api.call(command);command.value=99;const b=s.api.call({type:'query',value:2});assert.deepEqual(await Promise.all([a,b]),[1,2]);assert.equal(s.writes.length,2);});
test('timeout prevents overwrite until late acknowledgement',async()=>{const s=setup({respond:false});await assert.rejects(s.api.call({type:'query'},{timeoutMs:5}),/outcome_unknown/);await assert.rejects(s.api.call({type:'query'}),/previous_command_pending/);assert.equal(s.writes.length,1);s.ack();await assert.rejects(s.api.call({type:'query'},{timeoutMs:5}),/outcome_unknown/);assert.equal(s.writes.length,2);});
