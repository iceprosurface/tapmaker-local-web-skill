from __future__ import annotations

PLAYER_SCRIPT_URL = "https://tapcode-sce.spark.xd.com/src/web/src/index.min.js"
TAILWIND_BROWSER_URL = "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"
COMPONENT_TREE_MARKER = "__TAPMAKER_COMPONENT_TREE__"
ORIENTATION_SIZES = {
    "landscape": (844, 390),
    "portrait": (390, 844),
}

INDEX_HTML = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>TapMaker 本地预览</title>
  <style>
    :root {{
      --tapmaker-viewport-width: __TAPMAKER_VIEWPORT_WIDTH__;
      --tapmaker-viewport-height: __TAPMAKER_VIEWPORT_HEIGHT__;
    }}
    html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; background: #080808; }}
    #canvas {{
      position: fixed !important; left: 50% !important; top: 50% !important;
      width: min(calc(var(--tapmaker-viewport-width) * 1px), 100vw,
        calc(100vh * var(--tapmaker-viewport-width) / var(--tapmaker-viewport-height))) !important;
      height: min(calc(var(--tapmaker-viewport-height) * 1px), 100vh,
        calc(100vw * var(--tapmaker-viewport-height) / var(--tapmaker-viewport-width))) !important;
      transform: translate(-50%, -50%) !important;
      border: 0; outline: 0; box-shadow: 0 0 0 1px #282828;
    }}
    #loading-screen {{ position: fixed; inset: 0; z-index: 10000; display: flex;
      align-items: center; justify-content: center; flex-direction: column; gap: 14px;
      color: #fff; background: #080808; font: 15px system-ui, sans-serif; }}
    #loading-screen.hidden {{ display: none; }}
    #loading-progress-bg {{ width: min(560px, 70vw); height: 8px; background: #333; }}
    #loading-progress-bar {{ width: 0; height: 100%; background: #4a9eff; }}
    #dialog-overlay {{ position: fixed; inset: 0; z-index: 20000; display: none;
      align-items: center; justify-content: center; background: rgba(0,0,0,.75); }}
    #dialog-overlay.visible {{ display: flex; }}
    #dialog-box {{ min-width: 320px; padding: 24px; color: #fff; background: #353545;
      font: 16px system-ui, sans-serif; text-align: center; }}
    #dialog-cancel.hidden {{ display: none; }}
    #tapmaker-diagnostics {{ position: fixed; top: 10px; left: 50%; z-index: 11000;
      display: none; width: min(720px, calc(100vw - 20px)); max-height: 35vh;
      box-sizing: border-box; overflow: auto; padding: 10px 14px; border: 1px solid #f7b955;
      border-radius: 6px; color: #fff3d6; background: rgba(91, 52, 0, .94);
      font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
      transform: translateX(-50%); white-space: pre-wrap; }}
    #tapmaker-diagnostics.visible {{ display: block; }}
    #tapmaker-inspector {{ display: none; }}
    @media (min-width: 1100px) {{
      body.tapmaker-inspector-enabled #canvas {{
        left: calc((100vw - 360px) / 2) !important;
        max-width: calc(100vw - 380px) !important;
      }}
      body.tapmaker-inspector-enabled #tapmaker-inspector {{ display: flex; }}
    }}
  </style>
</head>
<body class="__TAPMAKER_INSPECTOR_BODY_CLASS__" oncontextmenu="return false">
  <div id="loading-screen">
    <div id="loading-status">正在启动本地预览…</div>
    <div id="loading-progress-bg"><div id="loading-progress-bar"></div></div>
    <div id="loading-percent">0%</div>
  </div>
  <canvas id="canvas" tabindex="0"></canvas>
  <div id="version-info"></div>
  <div id="tapmaker-diagnostics" role="status"></div>
  <aside id="tapmaker-inspector"
    class="fixed inset-y-0 right-0 z-[12000] w-[360px] flex-col border-l border-slate-700 bg-slate-950 text-slate-100 shadow-2xl">
    <header class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
      <div><div class="text-sm font-semibold">组件树 <span class="ml-1 text-[10px] font-normal text-slate-500">Lua 调试桥</span></div><div id="tapmaker-tree-status" class="mt-0.5 text-xs text-slate-400">等待游戏上报…</div></div>
      <button id="tapmaker-tree-refresh" class="rounded bg-slate-800 px-2.5 py-1 text-xs hover:bg-slate-700">刷新</button>
    </header>
    <div id="tapmaker-component-tree" class="min-h-0 flex-1 overflow-auto p-3 font-mono text-xs"></div>
  </aside>
  <div id="dialog-overlay"><div id="dialog-box">
    <h2 id="dialog-title"></h2><p id="dialog-message"></p>
    <button id="dialog-confirm">重新加载</button><button id="dialog-cancel" class="hidden"></button>
  </div></div>
  <script>
    if (!new URLSearchParams(location.search).has('workbench_session')) {{
      const tailwindRuntime = document.createElement('script');
      tailwindRuntime.src = '{TAILWIND_BROWSER_URL}';
      document.head.appendChild(tailwindRuntime);
    }}
  </script>
  <script>
    (() => {{
      let revision = null;
      const diagnostics = document.getElementById('tapmaker-diagnostics');
      const treeContainer = document.getElementById('tapmaker-component-tree');
      const treeStatus = document.getElementById('tapmaker-tree-status');
      const treeRefresh = document.getElementById('tapmaker-tree-refresh');
      const workbenchSession = new URLSearchParams(location.search).get('workbench_session');
      if (workbenchSession) document.body.classList.remove('tapmaker-inspector-enabled');

      function emitWorkbench(type, payload) {{
        if (!workbenchSession || parent === window) return;
        parent.postMessage({{
          protocol: 'tscn-workbench',
          version: 1,
          sessionId: workbenchSession,
          type,
          payload,
        }}, location.origin);
      }}

      addEventListener('message', event => {{
        const message = event.data;
        if (event.origin !== location.origin || message?.protocol !== 'tscn-workbench'
          || message.version !== 1 || message.sessionId !== workbenchSession) return;
        if (message.type === 'runtime.reload') location.reload();
        if (message.type === 'runtime.focus') document.getElementById('canvas')?.focus();
      }});

      async function publishComponentTree(tree) {{
        await fetch('/__tapmaker/component-tree', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(tree),
        }});
        renderComponentTree(tree);
        emitWorkbench('runtime.tree', tree);
      }}

      function treeNodeElement(node, depth = 0) {{
        const details = document.createElement('details');
        details.open = depth === 0;
        details.className = 'ml-3 border-l border-slate-800 pl-2';
        const summary = document.createElement('summary');
        summary.className = 'cursor-pointer select-none py-0.5 text-sky-300';
        const identifier = node.id ?? node.runtime_id ?? '?';
        summary.textContent = `${{node.type || 'Unknown'}}#${{identifier}}`;
        details.appendChild(summary);
        const props = node.props || {{}};
        for (const [key, value] of Object.entries(props)) {{
          const row = document.createElement('div');
          row.className = 'ml-4 truncate py-0.5 text-slate-400';
          row.title = `${{key}}: ${{String(value)}}`;
          row.textContent = `${{key}}: ${{String(value)}}`;
          details.appendChild(row);
        }}
        let childrenMounted = false;
        const mountChildren = () => {{
          if (childrenMounted) return;
          childrenMounted = true;
          for (const child of node.children || []) details.appendChild(treeNodeElement(child, depth + 1));
        }};
        details.addEventListener('toggle', () => {{ if (details.open) mountChildren(); }});
        if (details.open) mountChildren();
        return details;
      }}

      function renderComponentTree(payload) {{
        if (!treeContainer) return;
        treeContainer.replaceChildren();
        const root = payload?.root || payload;
        if (!root || !root.type) {{
          treeStatus.textContent = payload?.warning || '尚未收到组件树';
          const warning = document.createElement('div');
          warning.className = 'rounded border border-amber-700 bg-amber-950/60 p-3 leading-5 text-amber-200';
          warning.textContent = payload?.warning || '启用 --inspect 后，UI.SetRoot() 会自动上报。';
          treeContainer.appendChild(warning);
          if (payload?.agent_prompt) {{
            const prompt = document.createElement('pre');
            prompt.className = 'mt-3 whitespace-pre-wrap rounded bg-slate-900 p-3 leading-5 text-slate-300';
            prompt.textContent = payload.agent_prompt;
            treeContainer.appendChild(prompt);
          }}
          return;
        }}
        const categoryCount = Object.keys(payload.categories || {{}}).length;
        treeStatus.textContent = `${{payload.component_count || '?'}} 个组件 · ${{categoryCount}} 类${{payload.truncated ? ' · 已截断' : ''}}`;
        treeContainer.appendChild(treeNodeElement(root));
      }}

      async function loadComponentTree() {{
        try {{
          const response = await fetch('/__tapmaker/component-tree', {{ cache: 'no-store' }});
          const payload = await response.json();
          renderComponentTree(payload);
          emitWorkbench('runtime.tree', payload);
        }} catch (_) {{
          if (treeStatus) treeStatus.textContent = '组件树读取失败';
        }}
      }}

      if (treeRefresh) treeRefresh.addEventListener('click', loadComponentTree);
      const originalConsoleLog = console.log.bind(console);
      console.log = (...args) => {{
        originalConsoleLog(...args);
        for (const value of args) {{
          if (typeof value !== 'string') continue;
          const markerIndex = value.indexOf('{COMPONENT_TREE_MARKER}');
          if (markerIndex < 0) continue;
          const payloadIndex = markerIndex + '{COMPONENT_TREE_MARKER}'.length;
          try {{ publishComponentTree(JSON.parse(value.slice(payloadIndex))); }}
          catch (error) {{ originalConsoleLog('[tapmaker-local] component tree parse failed', error); }}
        }}
      }};

      function applyRevision(next) {{
        if (revision === null) revision = next;
        else if (next !== revision) location.reload();
      }}

      function renderDiagnostics(status) {{
        const missing = status?.diagnostics?.missing_meta || [];
        if (!missing.length) {{
          diagnostics.classList.remove('visible');
          diagnostics.textContent = '';
          return;
        }}
        diagnostics.textContent =
          `缺少 .meta（${{missing.length}}）\\n` + missing.map(path => `• ${{path}}`).join('\\n');
        diagnostics.classList.add('visible');
      }}

      async function loadStatus() {{
        const response = await fetch('/__tapmaker/status', {{ cache: 'no-store' }});
        const status = await response.json();
        renderDiagnostics(status);
        applyRevision(status.revision);
        emitWorkbench('runtime.ready', {{ revision: status.revision }});
        if (status.inspector?.enabled) loadComponentTree();
      }}

      async function pollRevision() {{
        try {{
          const response = await fetch('/__tapmaker/revision', {{ cache: 'no-store' }});
          const next = await response.json();
          applyRevision(next.revision);
        }} catch (_) {{
        }} finally {{
          setTimeout(pollRevision, 1000);
        }}
      }}

      loadStatus().catch(() => {{}}).finally(() => {{
        if ('EventSource' in window) {{
          const events = new EventSource('/__tapmaker/events');
          events.addEventListener('revision', event => {{
            try {{ applyRevision(JSON.parse(event.data).revision); }} catch (_) {{}}
          }});
        }} else {{
          pollRevision();
        }}
      }});
    }})();
  </script>
  <script src="{PLAYER_SCRIPT_URL}"></script>
</body>
</html>
""".encode("utf-8")


def render_index(orientation: str, *, inspector: bool = False) -> bytes:
    width, height = ORIENTATION_SIZES[orientation]
    return INDEX_HTML.replace(
        b"__TAPMAKER_VIEWPORT_WIDTH__", str(width).encode()
    ).replace(
        b"__TAPMAKER_VIEWPORT_HEIGHT__", str(height).encode()
    ).replace(
        b"__TAPMAKER_INSPECTOR_BODY_CLASS__",
        b"tapmaker-inspector-enabled" if inspector else b"",
    )
