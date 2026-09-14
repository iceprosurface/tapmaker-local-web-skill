# 测试说明

以下命令从仓库根目录执行。应用接入和日常调用见 [本地命令桥使用指南](../skills/tapmaker-local-web/references/local-commands.md)。

## 协议与服务测试

需要 uv、支持内置 `node:test` 的 Node.js，以及 LuaJIT。

```bash
uv run --project skills/tapmaker-local-web/scripts python -m unittest discover -s tests -v
node --test tests/local_command.test.cjs
luajit tests/local_bridge_test.lua skills/tapmaker-local-web/scripts/src/tapmaker_local_web/local_bridge.lua
luajit examples/local-command-demo.lua
```

这些测试覆盖资源 manifest、HTTP 服务、命令排队、错误处理、Lua 白名单与 dispatcher 示例。

## 真实浏览器验收

需要 Chrome 的 CDP 接口，以及提供全局 `fetch` 和 `WebSocket` 的 Node.js（建议 Node.js 22+）。仅对独立 Demo 执行：脚本会重置得分、开始游戏并加分。

1. 用空闲端口启动示例，保留这个终端：

   ```bash
   uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web web \
     --code examples/demo-project --entry scripts/main.lua \
     --port 8875 --orientation landscape --no-open
   ```

2. 启动带独立用户目录和 CDP 端口的 Chrome，打开上述服务器输出的页面地址一次。以 macOS 为例，保留这个终端：

   ```bash
   browser_profile=$(mktemp -d)
   '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
     --user-data-dir="$browser_profile" --remote-debugging-port=9359 \
     --no-first-run --no-default-browser-check \
     'http://127.0.0.1:8875/?skip_login&canvas=full'
   ```

   其他系统替换 Chrome 可执行文件路径。需要无头运行时加 `--headless=new`。已有专用测试页面时直接复用，不重复启动。

3. 在另一个终端运行：

   ```bash
   CDP_URL=http://127.0.0.1:9359 PAGE_URL=http://127.0.0.1:8875/ \
     node tests/browser_bridge_smoke.cjs
   ```

   `CDP_URL` 是浏览器调试地址；`PAGE_URL` 用来匹配已经打开的页面。端口不同时同步替换。可设 `SCREENSHOT_PATH=/absolute/path/score-20.png` 保存 20 分时的截图。

脚本复用既有 target，不打开或导航页面；将视口设置为 844×390，验证重置后的查询、开始、20 次并发请求依序加分、非法命令拒绝和最终重置。它检查操作期间的浏览器与 Runtime 错误，忽略 favicon 404；启动阶段日志与截图另行检查。耗时输出用于观察，不设置性能门槛。

该脚本验证 CDP → JavaScript → Lua → UI 调用链。WebMCP 宿主的工具发现与调用，需要在支持它的客户端另行验证。

完成后关闭专用 Chrome，按 Ctrl-C 停止示例服务器。不要停止其他任务的进程。
