"""PROTOTYPE: three Godot-inspired workbench layouts for the Maker stage."""

from __future__ import annotations

import html
import json


def render_workbench(
    preview_url: str,
    *,
    project_name: str,
    entry: str,
    assets: list[str],
) -> bytes:
    """Three variants of the workbench, switchable with ``?variant=A|B|C``."""
    bootstrap = json.dumps(
        {"project": project_name, "entry": entry, "assets": assets},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    stage_url = html.escape(preview_url, quote=True)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TSCN Workbench Prototype</title>
  <style>
    :root {{ color-scheme: dark; font: 12px/1.4 Inter, system-ui, sans-serif; --bg:#202226; --panel:#292c31; --line:#3b3f46; --muted:#9197a1; --blue:#5c9ded; }}
    * {{ box-sizing:border-box }} html,body {{ margin:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:#d9dce1 }}
    button,input {{ font:inherit;color:inherit }} button {{ border:0;background:transparent;cursor:pointer }}
    .app {{ display:grid;width:100%;height:100%;grid-template-rows:38px minmax(0,1fr) 26px }}
    .topbar {{ display:flex;align-items:center;gap:12px;padding:0 10px;border-bottom:1px solid #15171a;background:#25282d }}
    .brand {{ display:flex;align-items:center;gap:8px;font-weight:700 }} .mark {{ width:18px;height:18px;border-radius:5px;background:linear-gradient(135deg,#69a9f7,#456ca8) }}
    .menus {{ display:flex;gap:2px }} .menus button,.tool-btn {{ padding:5px 8px;border-radius:4px }} .menus button:hover,.tool-btn:hover {{ background:#353941 }}
    .workspace-tabs {{ display:flex;gap:2px;margin:auto }} .workspace-tabs button {{ min-width:64px;padding:6px 12px;border-radius:4px;color:var(--muted) }} .workspace-tabs .active {{ background:#373c44;color:#fff }}
    .run {{ display:flex;gap:4px }} .run button {{ width:26px;height:26px;border-radius:5px;background:#30343a }}
    .body {{ display:grid;min-height:0 }}
    .left {{ display:grid;min-width:0;min-height:0;border-right:1px solid #17191c;background:#25282d }}
    .dock {{ min-width:0;min-height:0;display:flex;flex-direction:column;border-bottom:1px solid #17191c }}
    .dock-title {{ display:flex;align-items:center;gap:6px;min-height:30px;padding:0 9px;border-bottom:1px solid var(--line);background:#2d3036;font-weight:600 }}
    .dock-title span:last-child {{ margin-left:auto;color:#7f858e }}
    .dock-content {{ min-height:0;overflow:auto;padding:5px }}
    .tree-row,.file-row {{ display:flex;align-items:center;gap:6px;width:100%;padding:4px 7px;border-radius:3px;white-space:nowrap }}
    .tree-row:hover,.file-row:hover,.tree-row.selected {{ background:#38485d }} .indent-1 {{ padding-left:22px }} .indent-2 {{ padding-left:38px }}
    .type {{ color:#72a6de }} .muted {{ color:var(--muted) }}
    .filter {{ width:100%;margin-bottom:5px;padding:5px 7px;border:1px solid #41464f;border-radius:4px;outline:none;background:#1f2125 }}
    .inspector {{ padding:8px }} .section {{ margin-bottom:8px;border:1px solid #3b3f46;border-radius:4px;overflow:hidden }}
    .section h3 {{ margin:0;padding:6px 8px;background:#30343a;font-size:11px }} .property {{ display:grid;grid-template-columns:42% 58%;align-items:center;padding:5px 8px;border-top:1px solid #34383f }}
    .property input {{ width:100%;padding:3px 5px;border:1px solid #444a54;border-radius:3px;background:#22252a }}
    .main {{ display:grid;min-width:0;min-height:0;grid-template-rows:32px minmax(0,1fr) }}
    .main-tabs {{ display:flex;align-items:end;gap:2px;padding:0 8px;border-bottom:1px solid #15171a;background:#282b30 }}
    .main-tabs button {{ height:28px;padding:0 13px;color:var(--muted);border-bottom:2px solid transparent }} .main-tabs .active {{ color:#fff;border-color:var(--blue) }}
    .main-tools {{ margin-left:auto;display:flex;align-items:center;gap:3px;height:100% }}
    .surface {{ position:relative;min-height:0;overflow:hidden;background:#181a1e }}
    .view {{ position:absolute;inset:0;display:none }} .view.active {{ display:block }}
    #runtime-view {{ background:radial-gradient(circle at 50% 50%,#31353b 0,#1b1d21 62%) }}
    #stage-frame {{ width:100%;height:100%;border:0;background:#080808 }}
    #tilemap-view {{ padding:34px;background-color:#1d2024;background-image:linear-gradient(#2a2e34 1px,transparent 1px),linear-gradient(90deg,#2a2e34 1px,transparent 1px);background-size:24px 24px }}
    .tile-card {{ width:288px;padding:16px;border:1px solid #48505b;border-radius:6px;background:#292d33;box-shadow:0 10px 35px #0006 }}
    .statusbar {{ display:flex;align-items:center;gap:14px;padding:0 8px;border-top:1px solid #111;background:#22252a;color:#aeb3bb }} .statusbar span:last-child {{ margin-left:auto }}
    .protocol-dot {{ width:7px;height:7px;border-radius:50%;background:#d39b42 }} .protocol-dot.ready {{ background:#67be7b;box-shadow:0 0 8px #67be7b88 }}
    .switcher {{ position:fixed;z-index:100000;left:50%;bottom:38px;display:flex;align-items:center;gap:8px;padding:5px 7px;border:1px solid #79808b;border-radius:999px;background:#111318ee;box-shadow:0 8px 30px #0009;transform:translateX(-50%) }}
    .switcher button {{ width:28px;height:28px;border-radius:50%;background:#303641 }} .switcher strong {{ min-width:180px;text-align:center }}
    .variant-note {{ position:absolute;z-index:5;right:12px;top:12px;padding:5px 8px;border:1px solid #ffffff25;border-radius:4px;background:#111b;color:#aeb5bf;pointer-events:none }}
    body[data-variant="A"] .body {{ grid-template-columns:310px minmax(0,1fr) }}
    body[data-variant="A"] .left {{ grid-template-rows:52% 48% }} body[data-variant="A"] #inspector-dock {{ display:none }}
    body[data-variant="B"] .body {{ grid-template-columns:230px minmax(0,1fr) }}
    body[data-variant="B"] .left {{ grid-template-rows:34px minmax(0,1fr) }} body[data-variant="B"] #files-dock,body[data-variant="B"] #inspector-dock {{ display:none }}
    body[data-variant="B"] #scene-dock {{ border:0 }} body[data-variant="B"] #scene-dock .dock-title {{ display:none }}
    body[data-variant="B"] .left::before {{ content:"SCENE  FILES  INSPECT";padding:9px;color:#8e949d;word-spacing:10px;border-bottom:1px solid #17191c;font-size:10px }}
    body[data-variant="C"] .body {{ grid-template-columns:370px minmax(0,1fr) }}
    body[data-variant="C"] .left {{ grid-template-rows:44% 56% }} body[data-variant="C"] #files-dock {{ display:none }}
    body[data-variant="C"] #inspector-dock {{ display:flex;grid-row:1 }} body[data-variant="C"] #scene-dock {{ grid-row:2 }}
    @media (max-width:800px) {{ .body,body[data-variant] .body {{ grid-template-columns:220px minmax(0,1fr) }} .menus {{ display:none }} }}
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand"><span class="mark"></span>TSCN</div>
      <nav class="menus"><button>场景</button><button>编辑</button><button>视图</button><button>调试</button></nav>
      <nav class="workspace-tabs"><button class="active">2D</button><button>脚本</button><button>资源</button></nav>
      <div class="run"><button id="reload-stage" title="重新加载">↻</button><button title="运行">▶</button><button title="停止">■</button></div>
    </header>
    <div class="body">
      <aside class="left">
        <section class="dock" id="scene-dock"><div class="dock-title">场景树 <span id="node-count">等待运行时</span></div><div class="dock-content"><input class="filter" placeholder="筛选节点"><div id="scene-tree"><div class="tree-row muted">正在连接 Maker Session…</div></div></div></section>
        <section class="dock" id="files-dock"><div class="dock-title">文件系统 <span>res://</span></div><div class="dock-content" id="asset-list"></div></section>
        <section class="dock" id="inspector-dock"><div class="dock-title">检查器 <span>Panel#1</span></div><div class="dock-content inspector"><input class="filter" placeholder="筛选属性"><div class="section"><h3>布局</h3><label class="property"><span>Width</span><input value="100%"></label><label class="property"><span>Height</span><input value="100%"></label></div><div class="section"><h3>可见性</h3><label class="property"><span>Visible</span><input value="true"></label></div></div></section>
      </aside>
      <main class="main">
        <nav class="main-tabs"><button class="active" data-view="runtime-view">运行时</button><button data-view="tilemap-view">TileMap</button><div class="main-tools"><button class="tool-btn">−</button><button class="tool-btn">100%</button><button class="tool-btn">+</button></div></nav>
        <div class="surface">
          <div class="variant-note" id="variant-note"></div>
          <section class="view active" id="runtime-view"><iframe id="stage-frame" title="Maker Runtime" src="{stage_url}"></iframe></section>
          <section class="view" id="tilemap-view"><div class="tile-card"><strong>TileMap 工作区</strong><p class="muted">MVP 先验证 Pane 注册和主区域切换。后续这里可以绑定独立 MakerSession 或专用编辑器。</p><div>当前图层：Ground</div><div>网格：24 × 24</div></div></section>
        </div>
      </main>
    </div>
    <footer class="statusbar"><span class="protocol-dot" id="protocol-dot"></span><span id="runtime-status">Maker Session 正在启动</span><span id="selection-status">未选择节点</span><span id="project-status"></span></footer>
  </div>
  <div class="switcher"><button id="previous-variant">←</button><strong id="variant-label"></strong><button id="next-variant">→</button></div>
  <script id="bootstrap" type="application/json">{bootstrap}</script>
  <script>
    (() => {{
      const protocol = 'tscn-workbench';
      const version = 1;
      const sessionId = 'stage-main';
      const variants = [
        {{ id:'A', name:'经典双 Dock' }},
        {{ id:'B', name:'紧凑导航' }},
        {{ id:'C', name:'检查器优先' }},
      ];
      const data = JSON.parse(document.querySelector('#bootstrap').textContent);
      const params = new URLSearchParams(location.search);
      let variant = variants.some(item => item.id === params.get('variant')) ? params.get('variant') : 'A';
      const frame = document.querySelector('#stage-frame');
      const tree = document.querySelector('#scene-tree');

      function setVariant(next) {{
        variant = next;
        document.body.dataset.variant = variant;
        const item = variants.find(candidate => candidate.id === variant);
        document.querySelector('#variant-label').textContent = `${{item.id}} — ${{item.name}}`;
        document.querySelector('#variant-note').textContent = `PROTOTYPE · ${{item.name}}`;
        params.set('variant', variant);
        history.replaceState(null, '', `${{location.pathname}}?${{params}}`);
      }}
      function cycle(delta) {{
        const index = variants.findIndex(item => item.id === variant);
        setVariant(variants[(index + delta + variants.length) % variants.length].id);
      }}
      document.querySelector('#previous-variant').onclick = () => cycle(-1);
      document.querySelector('#next-variant').onclick = () => cycle(1);
      addEventListener('keydown', event => {{
        if (['INPUT','TEXTAREA'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable) return;
        if (event.key === 'ArrowLeft') cycle(-1);
        if (event.key === 'ArrowRight') cycle(1);
      }});

      function nodeRows(node, depth = 0) {{
        const row = document.createElement('button');
        row.className = `tree-row ${{depth ? `indent-${{Math.min(depth,2)}}` : 'selected'}}`;
        row.innerHTML = `<span>▾</span><span class="type">${{node.type || 'Node'}}</span><span>#${{node.id ?? node.runtime_id ?? '?'}}</span>`;
        row.onclick = () => document.querySelector('#selection-status').textContent = `选择：${{node.type}}#${{node.id ?? node.runtime_id}}`;
        const result = [row];
        for (const child of node.children || []) result.push(...nodeRows(child, depth + 1));
        return result;
      }}
      function renderTree(payload) {{
        if (!payload?.root) return;
        tree.replaceChildren(...nodeRows(payload.root));
        document.querySelector('#node-count').textContent = `${{payload.component_count || '?'}} 节点`;
      }}
      addEventListener('message', event => {{
        if (event.origin !== location.origin) return;
        const message = event.data;
        if (message?.protocol !== protocol || message.version !== version || message.sessionId !== sessionId) return;
        if (message.type === 'runtime.ready') {{
          document.querySelector('#protocol-dot').classList.add('ready');
          document.querySelector('#runtime-status').textContent = 'Maker Session 已连接';
        }}
        if (message.type === 'runtime.tree') renderTree(message.payload);
      }});
      document.querySelector('#reload-stage').onclick = () => frame.contentWindow.postMessage({{protocol,version,sessionId,type:'runtime.reload'}}, location.origin);
      document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => {{
        document.querySelectorAll('[data-view]').forEach(item => item.classList.toggle('active', item === button));
        document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === button.dataset.view));
      }});
      document.querySelector('#asset-list').replaceChildren(...data.assets.map(path => {{
        const row = document.createElement('button'); row.className = 'file-row'; row.textContent = path.endsWith('.lua') ? `◇ ${{path}}` : `▧ ${{path}}`; return row;
      }}));
      document.querySelector('#project-status').textContent = `${{data.project}} · ${{data.entry}}`;
      setVariant(variant);
    }})();
  </script>
</body>
</html>'''.encode('utf-8')
