(() => {
  const prefix = '/home/web_user/update/' + location.host.replaceAll(':', '_') + '/savedata/0/saves/tapmaker-local-';
  let queue = Promise.resolve(), outstanding = null;
  const pause = () => new Promise(resolve => setTimeout(resolve, 30));
  const timeout = options => {
    const value = options?.timeoutMs ?? 10000;
    if (!Number.isFinite(value) || value <= 0 || value > 120000) throw Error('invalid_timeout');
    return value;
  };
  const read = name => JSON.parse(Module.FS.readFile(prefix + name + '.json', {encoding: 'utf8'}).replace(/\0+$/, ''));
  async function ready(options) {
    const deadline = Date.now() + timeout(options);
    while (Date.now() < deadline) {
      const loading = document.getElementById('loading-screen');
      if ((!loading || loading.classList.contains('hidden')) && globalThis.Module?.FS) {
        try { const registration = read('registration'); if (registration && typeof registration === 'object') return registration; } catch {}
      }
      await pause();
    }
    throw Error('local_bridge_not_ready');
  }
  function call(command, options) {
    let envelope, duration;
    try {
      duration = timeout(options);
      if (!command || Array.isArray(command) || typeof command.type !== 'string') throw Error('invalid_command');
      envelope = JSON.stringify({id: crypto.randomUUID(), command});
      if (new TextEncoder().encode(envelope).length > 4096) throw Error('command_too_large');
    } catch (error) { return Promise.reject(error); }
    const execute = async () => {
      const allowed = await ready({timeoutMs: duration});
      const request = JSON.parse(envelope);
      if (!Object.hasOwn(allowed, request.command.type) || allowed[request.command.type] !== true) throw Error('unsupported_command');
      if (outstanding) {
        let acknowledged = false;
        try { acknowledged = read('response').id === outstanding; } catch {}
        if (!acknowledged) throw Error('previous_command_pending');
        outstanding = null;
      }
      // 当前 WasmFS 的 writeFile 不覆盖已有文件，先清空已注册的邮箱。
      Module.FS.truncate(prefix + 'request.json', 0);
      Module.FS.writeFile(prefix + 'request.json', envelope);
      outstanding = request.id;
      const deadline = Date.now() + duration;
      while (Date.now() < deadline) {
        await pause();
        let response;
        try { response = read('response'); } catch { continue; }
        if (response.id === request.id) { outstanding = null; return response.result; }
      }
      throw Error('local_command_timeout_outcome_unknown');
    };
    const result = queue.then(execute);
    queue = result.catch(() => {});
    return result;
  }
  window.tapmakerLocal = Object.freeze({ready, call});
  const context = navigator.modelContext || document.modelContext;
  if (context?.registerTool) {
    try { context.registerTool({
      name: 'tapmaker_local_command',
      description: 'Invoke an application-whitelisted command through the local Lua dispatcher.',
      inputSchema: {type: 'object', properties: {command: {type: 'object', properties: {type: {type: 'string'}}, required: ['type']}}, required: ['command']},
      execute: async ({command}) => ({content: [{type: 'text', text: JSON.stringify(await call(command))}]})
    }); } catch (error) { console.warn('[tapmaker-local] WebMCP registration unavailable', error); }
  }
})();
