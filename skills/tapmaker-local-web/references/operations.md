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

浏览器每秒读取 `/__tapmaker/revision`。文件签名变化后，服务端复用未变资源的已有记录，更新 manifest client 并触发整页重载。localhost version 固定为 `local`，与 `version.toml` 无关；不要通过递增它来规避旧 manifest，否则会破坏稳定的 OPFS 资源缓存命名空间。

## 诊断顺序

### CLI 没有 `web`

运行 `uv run --project <skill-dir>/scripts tapmaker-local-web --help`。若无法识别命令，先确认 Skill 安装完整且本机可用 Python 3.11+ 与 uv；不要转而调用未审计的全局 Maker 工具。

### `--runtime local` 报未同步

执行 `web-runtime sync` 后重新运行 `status`。同步需要 `UrhoXRuntime.js`、`UrhoXRuntime.wasm` 和 `UrhoXRuntime.data` 三件都通过大小与 CRC32 校验；只存在 `.wasm` 不算可用 Runtime。

### Lua 入口无效

`--entry` 必须是相对于 `--code` 的现有文件，不能为绝对路径、包含 `..` 或越出项目目录。可以用 `--no-platform-mock` 做 Runtime 原始行为的最小差分诊断，但它不能修复错误的 entry。

### 修改后没有重载

直接请求 `/__tapmaker/revision`，确认 revision 是否改变，再检查文件是否位于 `--code` 指定的目录中。热重载是整页 reload，不是 Lua 函数级热替换；内存状态重置是预期行为。

### 资源请求或 Runtime WARNING

先建立可重复的浏览器日志或 HTTP 反馈环，再分类：

1. 项目资源的 manifest、UUID、CRC、`fs_path` 或 `/assets/...` 失败属于本地映射问题。
2. 官方 engine-startup、engine-res 或 official-res 告警要与同项目远程预览差分；线上同样出现且资源可用时，记录为官方 Runtime 告警。
3. 以代表性图片、音频、材质或脚本的实际加载结果为准，不以关键词数量代替功能验收。
