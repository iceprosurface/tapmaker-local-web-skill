---
name: tapmaker-local-web
description: 根据本地项目目录和 Lua entry 路径启动、验收和排查本地 UrhoX Web 预览。用户要求无需上传即可预览本地代码、检查 Lua/UI/资源热重载或诊断 manifest 时使用；不用于远程 Maker 构建或正式发布。
---

# TapMaker 本地 Web

将当前 worktree 的 Lua 和资源直接暴露给官方 UrhoX Web Player，建立不物化部署缓存、不修改 Git、不上传、不递增版本的日常反馈环。可执行实现位于本 Skill 的 `scripts/` 目录，安装 Skill 时会一起下载。

## 执行流程

1. 取得两个必需输入：本地项目根目录绝对路径、相对于该目录的 Lua `entry`。不读取或要求真实 Maker project id。
2. 确认本地项目目录属于用户放入任务范围的有效 worktree。读取该目录适用的 `AGENTS.md`；不读取该目录之外的其他项目、部署缓存或凭据。
3. 将包含本 `SKILL.md` 的目录解析为 Skill 根绝对路径。下文 `<skill-dir>` 和 `<code-dir>` 必须替换为实际绝对路径。
4. 运行 `uv run --project <skill-dir>/scripts tapmaker-local-web web-runtime status`。未同步时执行对应的 `web-runtime sync`；这只更新用户级 Runtime 缓存，不访问 Maker 项目。
5. 根据项目或用户要求选择方向：横屏使用默认的 `--orientation landscape`，竖屏使用 `--orientation portrait`；无法从任务内项目代码、配置或截图判断时才沿用横屏默认值。用可持续会话启动 `uv run --project <skill-dir>/scripts tapmaker-local-web web --code <code-dir> --entry <entry> --orientation <orientation> --no-open`。保留服务器输出中的实际 URL；不要猜测端口或吞掉启动错误。
6. 需要功能验收时，用可用的浏览器自动化打开该 URL。横屏按手机 CSS viewport `844 × 390` 验收，竖屏按 `390 × 844` 验收；其他尺寸只在任务涉及它们时切换并汇报。
7. 至少确认项目入口已执行、目标功能可见或可操作、本次涉及的代表性资源实际加载，且浏览器与引擎日志没有相关 `ERROR`。修改一个任务内文件后，确认页面能在 revision 变化后自动重载。
8. 验收后停止服务器，除非用户明确要求保持运行。最终回报 entry、URL、Runtime 模式、验收结果和是否已停止；不在公开回报中暴露本机绝对路径。

## 行为边界

- `--runtime auto` 是默认值：优先本地 Runtime，无缓存时使用 CDN。只在验证本地缓存时用 `local`，在差分 Runtime 缓存问题时用 `remote`。
- 默认的平台 mock 提供本地用户 `900000001`、昵称与内存云值。它只是游戏侧契约替身，不代表真实登录或云存档。普通本地测试不禁用；只在排查 Runtime 原始行为时使用 `--no-platform-mock`。
- Runtime 同步只本地化 `UrhoXRuntime.js`、`UrhoXRuntime.wasm` 和 `UrhoXRuntime.data`。Player 外壳、engine-res、official-res 仍可能访问 CDN；不得宣称完全离线。
- 本地页面不证明远程项目绑定、计费归属、真实平台账号或 production 行为。需要真实登录、云端数据、排行榜、广告、平台权限或远程预览时，停止本地结论，转入仓库的 Maker test 发布流程；未经授权不得触发远程构建。
- 将 host 绑定为 `0.0.0.0` 会把无登录保护、允许跨源读取的项目服务暴露给局域网。只在用户要求其他设备访问且网络可信时使用。

启动参数、资源诊断或常见错误需要更多细节时，读取 [操作与排错](references/operations.md)。
