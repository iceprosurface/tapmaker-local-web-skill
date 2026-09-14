# 本地语义命令桥

`tapmaker_local_command` 是可选的 WebMCP 工具名。没有 WebMCP 的浏览器也可通过 `window.tapmakerLocal.call(command)` 使用同一通道。

调用链：浏览器 / CDP → JavaScript 串行队列 → Emscripten 内存文件系统请求 → Lua Update 轮询 → 应用 dispatcher → JSON 响应。没有远程 Maker 调用，也不新增执行宿主命令的 HTTP 接口。

## 应用接入

Runner 只在本地 manifest 注入 `__tapmaker_local_build_info.lua` 和 `__tapmaker_local_bridge.lua`，不写入源码树；禁用平台 mock 时仍可用。应用不要提交这两个模块到生产资源。以下代码在应用初始化后执行：

```lua
local pollLocal
local isLocal, build = pcall(require, "__tapmaker_local_build_info")
if isLocal and build.is_local == true then
    pollLocal = require("__tapmaker_local_bridge").register(build, {
        query = true,
        add_score = true,
    }, dispatch)
end
-- 在现有 Update 回调中：
if pollLocal then pollLocal() end
```

`dispatch(command)` 必须是 UI 和命令行入口共用的应用服务。命令名由应用显式白名单确定，参数校验也由应用负责。不能用任意 Lua 求值、模块加载、路径读写或宿主进程访问作为领域命令。修改动作必须走正式状态更新与 UI 订阅，不得只改控件或伪造测试结果。

返回值必须可 JSON 序列化；`nil` 返回 JSON null。异常返回 `{ok=false,error="dispatch_failed",message=...}`。序列化失败返回 `result_not_serializable`。长任务先返回任务标识，随后通过只读查询确认完成；桥不会推进模拟时间或自动等待领域任务完成。

可复用的领域 dispatcher 示例见仓库 `examples/local-command-demo.lua`，它可直接由 LuaJIT 执行，也可作为模块供游戏 UI 调用。

## 浏览器与 CDP

```javascript
await window.tapmakerLocal.ready({timeoutMs: 10000}); // 返回命令白名单
await window.tapmakerLocal.call({type: 'query'});
await window.tapmakerLocal.call({type: 'add_score', amount: 2});
```

CDP 对既有 target 使用 `Runtime.evaluate`，参数示例：

```json
{"expression":"window.tapmakerLocal.call({type:'query'})","awaitPromise":true,"returnByValue":true}
```

以 `--no-open` 启动 Runner；优先复用现有页面，首次没有目标时只打开一次。后续查询不重新打开页面、不抢焦点。语义状态用于断言行为；截图用于布局、绘制、动画的补充验收。测试 Lua 命令契约后仍需验证真实 Web Player 中同一调用可达、返回可序列化且 UI 跟随真实状态更新。

## 超时与并发

- `ready` 等待加载层隐藏、Module.FS 和 Lua 注册文件就绪，默认最多 10 秒。应用没注册或引擎没启动时返回 `local_bridge_not_ready`。
- `call` 默认分别给就绪与响应阶段 10 秒；可传 `{timeoutMs: 30000}`，范围大于 0 且不超过 120000 毫秒。排队时间不包含在这两个阶段内。
- 调用串行执行，输入在调用时快照；请求 UTF-8 总大小上限 4096 字节。未知命令在写入前拒绝，Lua 再做白名单检查。
- 响应按请求 ID 配对，忽略旧响应。`local_command_timeout_outcome_unknown` 表示动作可能已经执行，**不要自动重试写操作**。
- 超时后阻止覆盖待处理请求，后续调用可能得到 `previous_command_pending`。原请求收到迟到响应后才可继续；也可重新加载页面，但这会重启应用，不能当成成功证明。
- Lua 对连续重复请求 ID 不重复执行；这不是跨刷新、跨会话的持久幂等机制。
- 源码修改仍触发原有自动重载。验收期间保持源码稳定；页面重载会终止当前调用。

## 运行边界

当前官方 Web Runtime 的文件映射为 `/home/web_user/update/<host冒号替换为下划线>/savedata/0/saves/`，Lua 使用 `saves/`。这里的 `0` 是 Runtime 路径约定，不是平台 mock 用户 ID。升级 Runtime 后需验证这个映射；路径变化会使桥无法就绪。

这是一条开发调试通道，不是身份认证机制；同页面脚本也可调用白名单。生产包必须不含生成模块、JS bootstrap 或 WebMCP 注册。不要把本地 mock 验收当成真实平台验收。

## 实测与复现

仓库 `examples/demo-project` 已接入 `query/start/add_score/reset`，复用 UI 按钮的同一组应用操作。在单独端口启动该示例，并在启用 CDP 的隔离浏览器中打开一次页面后运行：

```bash
CDP_URL=http://127.0.0.1:9359 PAGE_URL=http://127.0.0.1:8875/ node tests/browser_bridge_smoke.cjs
```

脚本会修改示例得分，仅用于独立 Demo；复用既有 target，不打开新页面。可设 `SCREENSHOT_PATH` 保存 20 分时的截图。它验证初始查询、开始、20 次并发请求串行执行、非法命令拒绝及重置。

2026-09-14 在本地 Runtime `1.31.5-03435566`、无头 Chrome、844×390 下通过真实 Web Player 验收；20 次调用约 854ms，仅是该环境的观测值，不是性能保证。截图得分与查询一致。没有 Lua/JavaScript 异常，页面 favicon 请求返回 404。WebMCP 宿主发现/调用需在支持它的客户端另行验收，本次通过 CDP 验证 JS→Lua→UI。

实测修复：当前 Runtime 的 `FS.writeFile` 对已有邮箱不会按预期替换内容，发送前必须 `FS.truncate(path, 0)`；仅模拟标准文件覆盖行为的单元测试无法发现这个兼容问题。
