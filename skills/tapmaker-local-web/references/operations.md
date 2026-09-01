# 操作与排错

## 常用命令

```bash
# 将下面三个值替换为实际值
TAPMAKER_LOCAL_SKILL_DIR=/path/to/tapmaker-local-web-skill
TAPMAKER_LOCAL_CODE_DIR=/path/to/local-project
TAPMAKER_ENTRY=scripts/main.lua

# 查看和同步官方 Web Runtime 三件套
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web web-runtime status
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web web-runtime sync

# 默认预览：自动选 Runtime，启用本地平台 mock
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --no-open

# 竖屏预览：Player 参数和 Canvas 比例都会切换为竖屏
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --orientation portrait --no-open

# 强制验证本地 Runtime 缓存
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --runtime local --no-open

# 用远程 Runtime 差分本地缓存问题
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --runtime remote --no-open

# 排查 skip_login 下的 Runtime 原始平台行为
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --no-platform-mock --no-open

# 更换端口
uv run --project "$TAPMAKER_LOCAL_SKILL_DIR/scripts" tapmaker-local-web \
  web --code "$TAPMAKER_LOCAL_CODE_DIR" \
  --entry "$TAPMAKER_ENTRY" --port 8766 --no-open
```

Runtime 缓存默认位于：

- macOS：`~/Library/Caches/TapMaker/web-runtime`
- 其他系统：`$XDG_CACHE_HOME/tapmaker/web-runtime`，未设置时使用 `~/.cache/tapmaker/web-runtime`

可用 `TAPMAKER_WEB_RUNTIME_CACHE` 或命令的 `--cache`/`--runtime-cache` 覆盖。`sync`、`status` 和 `web` 必须指向同一个自定义缓存。

## 运行机制

`LocalWebProject` 将 `--code` 目录映射为一个只读本地资源树，使用 `.meta` UUID、文件 CRC32 和大小构造 Maker 兼容 manifest。`.meta` 不作为资源发布；`.git`、`.project`、`.maker-mcp`、`.env`、`.venv`、`node_modules`、系统元数据和其他隐藏路径不进入 manifest。

服务端通过操作系统文件事件监听挂载目录，只在变化后扫描；浏览器通过
`/__tapmaker/events` 的 SSE 事件流接收 revision。不支持 SSE 时才每秒轮询
`/__tapmaker/revision`，轮询接口只读取内存状态。文件签名变化后，服务端复用未变资源的已有记录，更新 manifest client 并触发整页重载。localhost version 固定为 `local`，与 `version.toml` 无关；不要通过递增它来规避旧 manifest，否则会破坏稳定的 OPFS 资源缓存命名空间。

已加载文件缺少同名 `.meta` 时，终端和预览页顶部会列出相对路径，
`/__tapmaker/status` 也会在 `diagnostics.missing_meta` 返回完整列表。新增或删除
`.meta` 会触发刷新；`.meta` 本身仍不进入 manifest。

本地 Runtime 三件套使用 ETag 和 `Cache-Control: private, no-cache`。浏览器刷新时仍会
向 localhost 重验证，但内容未变化时返回 304 并复用浏览器缓存，不会再次传输大型
`.wasm`/`.data` 响应。项目资源 URL 包含 UUID 与 CRC，因此使用一年期 immutable
缓存；HTML、manifest、revision、status 与 SSE 保持 `no-store`。

## 诊断顺序

### CLI 没有 `web`

运行 `uv run --project <skill-dir>/scripts tapmaker-local-web --help`。若无法识别命令，先确认 Skill 安装完整且本机可用 Python 3.11+ 与 uv；不要转而调用未审计的全局 Maker 工具。

### `--runtime local` 报未同步

执行 `web-runtime sync` 后重新运行 `status`。同步需要 `UrhoXRuntime.js`、`UrhoXRuntime.wasm` 和 `UrhoXRuntime.data` 三件都通过大小与 CRC32 校验；只存在 `.wasm` 不算可用 Runtime。

### Lua 入口无效

`--entry` 必须是相对于 `--code` 的现有文件，不能为绝对路径、包含 `..` 或越出项目目录。可以用 `--no-platform-mock` 做 Runtime 原始行为的最小差分诊断，但它不能修复错误的 entry。

### 修改后没有重载

直接请求 `/__tapmaker/revision`，确认 revision 是否改变，再检查文件是否位于 `--code` 指定的目录中。热重载是整页 reload，不是 Lua 函数级热替换；内存状态重置是预期行为。

### 预览页提示缺少 `.meta`

为提示中的每个相对资源路径补充同名 `<文件名>.meta`。如果文件不应加载，应将它移出
挂载目录或加入现有安全过滤范围；不要只为隐藏提示而创建无效 JSON。保存有效 `.meta`
后，文件监听器会更新 UUID、清除诊断并自动重载页面。

### 资源请求或 Runtime WARNING

先建立可重复的浏览器日志或 HTTP 反馈环，再分类：

1. 项目资源的 manifest、UUID、CRC、`fs_path` 或 `/assets/...` 失败属于本地映射问题。
2. 官方 engine-startup、engine-res 或 official-res 告警要与同项目远程预览差分；线上同样出现且资源可用时，记录为官方 Runtime 告警。
3. 以代表性图片、音频、材质或脚本的实际加载结果为准，不以关键词数量代替功能验收。
