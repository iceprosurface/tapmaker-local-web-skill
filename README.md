# TapMaker Local Web

TapMaker 项目的本地 UrhoX Web 预览器，同时也是一个可安装的 Agent Skill。

它直接把本地项目目录转换成官方 Web Player 可读取的 manifest 和资源接口，用于快速检查 Lua、UI、图片、音频、材质及其他运行时资源。整个反馈环不需要真实 Maker 项目、不需要 project id、不需要部署缓存，也不会上传本地项目文件或递增发布版本。

> [!NOTE]
> **官方已支持本地预览，建议优先使用官方工具链。** 截至 2026-09-24，官方 [`@taptap/maker`](https://www.npmjs.com/package/@taptap/maker) CLI（v0.0.34+）已正式提供本地预览与 Maker 本地控制台：
>
> ```bash
> # 官方本地预览：原生 Runtime 窗口直读项目原目录，不提交、不上传、不远端构建
> npx -y -p @taptap/maker taptap-maker preview install --target-dir /absolute/path/to/project --json
> npx -y -p @taptap/maker taptap-maker preview start --target-dir /absolute/path/to/project --json
>
> # Maker 本地控制台：本机网页工具台，集中本地预览、远端构建、测试二维码与日志
> npx -y -p @taptap/maker taptap-maker console open --target-dir /absolute/path/to/project
> ```
>
> 官方方案使用原生 UrhoX Runtime，更接近真实运行环境，并支持联机项目连接官方测试服。新用户建议优先使用官方工具；本项目继续聚焦浏览器 Web Player 场景，并对齐官方 `preview` 工作流与联机测试服直连（见[下文](#复刻官方-preview-工作流实验性)）。

> [!IMPORTANT]
> **免责声明：**本项目是由社区开发者独立维护的非官方开源工具，仅供软件开发、技术研究、学习交流和本地调试使用。本项目与 TapTap、TapMaker 及其运营方、关联公司之间不存在隶属、授权、合作、赞助、认可或背书关系，也不代表 TapTap 或 TapMaker 官方立场。项目中出现的 TapTap、TapMaker、UrhoX 等名称及相关商标、产品标识和服务归其各自权利人所有。使用者应自行遵守适用的服务条款、开发者协议、软件许可和法律法规，并自行承担使用本项目产生的风险与责任。本项目按“现状”提供，不对可用性、兼容性、数据安全或特定用途作任何明示或默示保证，不建议将其作为正式发布、生产部署或官方验收依据。

## 它解决什么问题

本项目立项时，Maker 还只有远程预览：日常修改一行 Lua、一张图片或一个材质，也要走完整的测试、物化、同步和远程构建链路。如今官方 CLI 已内置本地预览（见顶部说明），这个痛点有了官方解法；下文描述本项目保留的浏览器 Web Player 反馈环。

TapMaker Local Web 保留官方 UrhoX Web Player 和 Runtime，只把项目来源替换为 localhost：

```text
本地项目目录
    ↓ 扫描、过滤、计算 UUID/CRC
本地 manifest 与 /assets/... 接口
    ↓
官方 UrhoX Web Player
    ↓
浏览器预览与自动重载
```

真正必需的输入只有两个：

- 本地项目根目录，例如 `/path/to/game-content`
- 相对该目录的 Lua 入口，例如 `scripts/main.lua`

## 与官方本地预览的差异

日常开发和贴近真机的验收建议直接使用官方 `taptap-maker preview`。本项目保留的价值集中在「浏览器里的官方 Web Player」，适合无法安装本机 Runtime、或需要保存即自动重载的纯浏览器工作流：

| | 官方 `taptap-maker preview` | TapMaker Local Web |
| --- | --- | --- |
| 形态 | 本机原生 UrhoX Runtime 窗口 | 浏览器中的官方 Web Player |
| 项目来源 | 优先直读项目原目录，必要时走受管理副本 | 本地 manifest 与 `/assets/...` 接口 |
| 会话管理 | `preview start/status/stop` | 同名动词：`preview start --detached/status/stop` |
| 运行观测 | Runtime 日志、`preview logs/check` | 页面上报日志、里程碑与错误计数，`preview logs/check` |
| 截图 | `preview screenshot` | 同名动词，经 SSE 命令让页面回传 canvas PNG |
| 联机项目 | 可连官方测试服 | 同样支持：自动申请测试游戏并经 `directConnectParams` 直连 |
| 改动反馈 | `preview refresh` 手动重启 | `preview refresh` 重载 + 保存后自动整页重载 |
| 依赖 | 本机 Runtime（`~/.taptap-maker/runtime/`） | Python 3.11+ 与 uv，可选本地 Runtime 缓存 |
| 维护 | 官方持续迭代 | 社区维护，优先保证既有工作流兼容 |

## 复刻官方 preview 工作流（实验性）

对齐官方 `taptap-maker preview` 的动词，把浏览器页当作预览窗口：

```bash
# 后台启动会话（--port 0 自动分配端口）
tapmaker-local-web preview start \
  --code /absolute/path/to/game-content \
  --entry scripts/main.lua --detached --no-open --port 0

# 管理动词（与 start 相同的 --code/--entry 定位会话）
tapmaker-local-web preview status    # 会话与健康检查
tapmaker-local-web preview refresh   # 通知页面整页重载
tapmaker-local-web preview logs --follow   # 页面上报的运行日志
tapmaker-local-web preview screenshot --out shot.png  # 页面 canvas 截图
tapmaker-local-web preview check     # 启动里程碑、错误计数与诊断汇总
tapmaker-local-web preview stop      # 停止会话
```

同时提供 `--size WxH` 视口预设（100–4096，对应官方控制台的窗口尺寸）。

实现要点：

- 预览页在 Player 脚本前注入控制脚本：包装 console、捕获 window 错误、上报里程碑，并为 WebGL
  上下文补 `preserveDrawingBuffer`，使 canvas 可随时截屏。
- 截图与重载通过 `/__tapmaker/events` 的 SSE `command` 事件下发，页面回传 PNG 后由 CLI 落盘；
  没有已连接页面时截图返回 409。
- 所有写操作（refresh/shutdown/screenshot/上报）要求 `X-TapMaker-Control` 令牌（存于会话
  `session.json` 并嵌入页面），loopback 绑定时同时校验 `Host` 头，防止跨站页面伪造本地请求。
- 会话状态写入用户缓存目录（macOS：`~/Library/Caches/TapMaker/local-web/<项目命名空间>/`），
  可用 `TAPMAKER_LOCAL_WEB_STATE_ROOT` 覆盖。

### 联机测试服直连

检测到联机/server 项目（`@runtime.multiplayer`、`@runtime.max_players`、`entry@server`、
`scripts/server_main.lua`/`server.lua`）时，启动会自动复刻官方鉴权与申请链路：

1. 复用官方 `taptap-maker login` 的本地 PAT（`~/.taptap-maker/pat.json`，或 `MAKER_PAT` 环境变量）
2. 用 PAT 换取测试服登录凭据（`maker.taptap.cn/api/v1/user/taptap-token` → `kid`/`mac_key`）
3. 连接官方入口 `wss://entrance-new-pd.spark.xd.com`，按 CreateMultiDebugGame 契约申请 `test` 标签测试游戏
4. 将 `{userId, connectInfo: {pod_ip, ws_port}}` 编码为 `directConnectParams` 追加到预览 URL，官方 Web Player 直接进入直连模式（联机模式下自动禁用本地平台 mock）

要求与限制：

- 需要项目已提交构建；测试版本依次取 `dist/latest.json`、`.project/project.json` 的 `version`，可用 `--project-version` 覆盖
- 服务端运行远端已构建版本；本地 server 代码修改需提交构建后才生效（客户端改动仍走本地热重载）
- `--multiplayer off` 可跳过测试服，按单机预览
- 会话期间 `preview refresh` 复用同一测试游戏；重启会话（stop 后 start）才申请新游戏
- PAT 与凭据只存在官方本地存储与本进程内存，不进入日志、URL 或会话记录

## 一键安装 Agent Skill

安装到 Codex 的用户级 Skill 目录：

```bash
npx skills add iceprosurface/tapmaker-local-web-skill -g -a codex -y
```

安装后重启 Codex，然后直接请求：

```text
使用 $tapmaker-local-web 预览本地项目。
代码目录：/absolute/path/to/game-content
入口：scripts/main.lua
```

Skill 自带完整的 Python 实现。Agent 不需要再从其他私有仓库复制代码，也不需要调用全局 Maker 工具。

也可以使用完整 GitHub 地址安装：

```bash
npx skills add https://github.com/iceprosurface/tapmaker-local-web-skill \
  --skill tapmaker-local-web -g -a codex -y
```

Skill 页面：[skills.sh/iceprosurface/tapmaker-local-web-skill/tapmaker-local-web](https://skills.sh/iceprosurface/tapmaker-local-web-skill/tapmaker-local-web)

## Clone 后直接运行

环境要求：

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 能访问官方 TapMaker CDN；本地化 Runtime 后仍有部分官方资源可能走 CDN

克隆并初始化：

```bash
git clone https://github.com/iceprosurface/tapmaker-local-web-skill.git
cd tapmaker-local-web-skill
uv sync --project skills/tapmaker-local-web/scripts
```

启动本地项目：

```bash
uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web \
  web \
  --code /absolute/path/to/game-content \
  --entry scripts/main.lua
```

默认会自动打开浏览器，地址通常是：

```text
http://127.0.0.1:8765/?skip_login&verbose=true&screen_orientation=landscape&entry=main.lua
```

保存本地脚本或资源后，页面会自动重新加载。按 `Ctrl-C` 停止服务。

## 运行脱敏 demo

仓库自带一个不包含真实游戏内容的最小 demo：

```bash
uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web \
  web \
  --code examples/demo-project \
  --entry scripts/main.lua \
  --runtime remote \
  --no-open
```

然后打开：

```text
http://127.0.0.1:8765/
```

demo 只使用公开的 `urhox-libs/UI` 组件和一份普通 JSON 资源，不包含真实游戏内容。页面提供“开始游戏”“+1 加分”和“重置”按钮，可直接验证 UI 渲染、点击响应、状态更新与源码热重载。

这个命令同时模拟 Maker 标准的两个资源根：

```text
scripts/main.lua  → main.lua
assets/demo.json  → demo.json
```

demo 还包含一个只用于本地扩展验收的 `config/local-preview.json`。显式传入三个资源根即可验证三目录合并：

```bash
uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web \
  web \
  --code examples/demo-project/assets \
  --code examples/demo-project/scripts \
  --code examples/demo-project/config \
  --entry scripts/main.lua \
  --runtime remote \
  --no-open
```

此时 `config/local-preview.json` 映射为 `local-preview.json`。三资源根是本地预览扩展，不代表当前 Maker MCP 的远端构建支持。

仓库测试会真正启动以上两种 demo 服务，通过 HTTP 检查 manifest 并下载代表性资源：

```bash
uv run --project skills/tapmaker-local-web/scripts \
  python -m unittest tests.test_demo -v
```

## CLI

### 启动预览

```bash
tapmaker-local-web web \
  --code <本地项目根目录> \
  --entry <相对 Lua 入口>
```

项目内容分别位于多个资源根时，可以重复传入 `--code`。命令行中的 `--entry` 可以相对于这些目录的共同父目录，也可以直接写相对于唯一资源根的 `main.lua`：

```bash
tapmaker-local-web web \
  --code /project/assets \
  --code /project/scripts \
  --code /project/config \
  --entry scripts/main.lua
```

每个 `--code` 都按 Maker `build.asset_dirs` 的语义成为独立资源根：`scripts/main.lua` 在 manifest 和 Player 中是 `main.lua`，`assets/image/hero.png` 是 `image/hero.png`。未显式传入的同级目录不会被扫描。

只传项目根目录时，会优先读取 `.project/settings.json` 的 `build.asset_dirs`；没有该配置时自动识别常规的 `assets` 和 `scripts`。当前 Maker 官方标准是 `assets + scripts`；第三个及更多 `--code` 属于本地预览扩展兼容，不表示项目能通过当前 Maker MCP 的远端构建检查。

主要选项：

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--code` | 必填、可重复 | 要映射的本地项目或资源目录 |
| `--entry` | 必填 | 可相对项目根/共同父目录，或相对唯一资源根；运行时会去掉资源根前缀 |
| `--host` | `127.0.0.1` | HTTP 监听地址 |
| `--port` | `8765` | HTTP 监听端口 |
| `--no-open` | 关闭 | 不自动打开浏览器 |
| `--runtime auto\|local\|remote` | `auto` | 选择 Runtime 来源 |
| `--runtime-cache` | 用户缓存 | 覆盖 Runtime 缓存目录 |
| `--no-platform-mock` | 关闭 | 禁用本地账号、昵称和云值 mock |
| `--orientation landscape\|portrait` | `landscape` | 设置 Player 方向和 Canvas 比例 |

`--entry` 必须：

- 是相对路径
- 位于 `--code` 目录内
- 指向实际存在的文件
- 不包含 `..`

竖屏项目使用 `--orientation portrait`。预览 URL 会传递
`screen_orientation=portrait`，页面 Canvas 同时按 `390:844` 缩放；默认横屏按
`844:390` 缩放。

### 查看 Runtime

```bash
uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web \
  web-runtime status
```

### 同步 Runtime

```bash
uv run --project skills/tapmaker-local-web/scripts tapmaker-local-web \
  web-runtime sync
```

同步过程从官方 engine manifest 下载并校验：

- `UrhoXRuntime.js`
- `UrhoXRuntime.wasm`
- `UrhoXRuntime.data`

只有三个文件全部通过大小和 CRC32 校验，缓存才会被标记为可用。

默认缓存位置：

- macOS：`~/Library/Caches/TapMaker/web-runtime`
- Linux 等系统：`$XDG_CACHE_HOME/tapmaker/web-runtime`
- 未设置 `XDG_CACHE_HOME` 时：`~/.cache/tapmaker/web-runtime`

可以通过 `TAPMAKER_WEB_RUNTIME_CACHE` 或 CLI 缓存参数覆盖。

## Runtime 模式

### `auto`

默认模式。存在完整的本地 Runtime 缓存时使用本地三件套，否则回退到官方 CDN。

### `local`

强制使用本地缓存。如果尚未同步或缓存不完整，启动会直接失败，适合验证离线 Runtime 缓存是否正确。

### `remote`

强制使用官方 CDN Runtime，适合排查本地缓存版本或文件差异。

> 本地 Runtime 只覆盖核心三件套。Player 外壳、engine-res、official-res 和首次未缓存的官方资源仍可能访问 CDN，因此不能将它描述为完全离线模式。

## 本地资源映射

启动时，工具会：

1. 规范化并验证 `--code` 和 `--entry`。
2. 按 Maker `build.asset_dirs` 解析一个或多个独立资源根，并递归扫描它们。
3. 读取 `.meta` 中已有的 UUID；没有 UUID 时生成稳定的本地 UUID。
4. 计算资源 CRC32 和大小。
5. 为 Lua、JSON、XML、material、prefab 等启动资源标记 blocking group。
6. 生成 `settings.json`、`latest.json`、`project.json` 和项目 manifest。
7. 通过 `/assets/<uuid>-<crc>.<ext>` 按需读取本地文件。
8. 监听文件签名变化，更新 manifest client 并触发页面重载。

协议内部会生成一个不可逆的本地命名空间，用于隔离浏览器缓存。它不是 Maker project id，不会用于远程查询，也不会出现在启动参数中。

## 平台 mock

官方 Player 使用 `skip_login` 启动时没有真实 TapTap 用户。默认情况下，工具会在内存中包装入口，提供最小平台替身：

- 固定的本地测试用户
- 本地测试昵称
- 页面生命周期内的 `clientCloud:Get/Set`

原始入口会以内存别名 `__tapmaker_project_entry.lua` 提供，包装入口最后再加载它。包装代码不会写回本地项目。

这个 mock 只能验证游戏侧逻辑，不能证明以下能力正常：

- 真实 TapTap 登录
- 真实云存档
- 排行榜
- 广告
- 平台权限
- 远程项目绑定
- production 行为

需要观察没有 mock 的 Runtime 原始行为时，使用：

```bash
tapmaker-local-web web \
  --code /absolute/path/to/game-content \
  --entry scripts/main.lua \
  --no-platform-mock
```

## 热重载

服务端使用操作系统文件事件递归监听挂载目录。浏览器通过
`/__tapmaker/events` 的 Server-Sent Events 接收 revision；不支持 SSE 时才每秒读取一次
`/__tapmaker/revision`，该接口只读取内存状态，不扫描文件系统。

服务端会合并短时间内的连续文件事件。发现脚本、资源或对应 `.meta` 变化后才扫描一次，
并复用未变化资源的缓存记录：

1. 更新资源记录和 manifest client。
2. revision 递增。
3. 页面执行整页 reload。

已加载文件缺少同名 `.meta` 时，启动终端和预览页顶部都会列出相对资源路径。
新增或删除 `.meta` 同样会触发刷新；`.meta` 只提供 UUID，不会作为资源发布。

这是页面级重载，不是 Lua 函数级热替换，因此当前内存状态会重置。

localhost 的 Runtime version 固定为 `local`。变化的是 manifest client，而不是发布版本，从而保留稳定的 OPFS 资源缓存命名空间。

## 隐私与安全

公开仓库不包含任何真实游戏项目、部署缓存、Maker UUID、Token、凭据、私有服务地址或本机绝对路径。

运行时遵循以下规则：

- 默认只监听 `127.0.0.1`。
- 不读取 `--code` 目录之外的文件。
- 不接受包含 `..` 或越界的 entry。
- 不向 Maker 或其他项目服务上传本地文件。
- 不把本机代码目录写进浏览器 URL 或 `project.json`。
- 自动排除 `.git`、`.project`、`.maker-mcp`、`.env`、其他隐藏路径、`.venv`、`node_modules` 和 `__pycache__`。
- `.meta` 只用于读取资源 UUID，不作为资源本身发布。

仍然建议把 `--code` 指向项目根或明确的 Maker 资源目录。项目根模式只扫描解析出的资源根；所有资源根中未被过滤的普通文件都可能进入本地 manifest。

使用 `--host 0.0.0.0` 会让同一网络内的其他设备访问服务。该服务没有登录保护并允许跨源资源读取，只应在可信局域网中使用，不能暴露到公网。

## 本地 HTTP 接口

| 路径 | 用途 |
| --- | --- |
| `/` | 官方 Player 外壳（含控制脚本注入） |
| `/latest.json` | 本地 Runtime 版本信息 |
| `/project.json` | 本地项目协议信息 |
| `/local/manifest-<client>.json` | 当前资源 manifest |
| `/assets/...` | 按需读取本地资源 |
| `/__tapmaker/revision` | 热重载 revision |
| `/__tapmaker/events` | revision 与 command 的 SSE 事件流 |
| `/__tapmaker/status` | revision 与资源诊断 |
| `/__tapmaker/health` | 会话存活与项目标识 |
| `/__tapmaker/check` | 启动里程碑、错误计数、诊断汇总 |
| `/__tapmaker/logs` | 页面上报日志尾部 |
| `/__tapmaker/screenshot(.png)` | 最近一次截图状态与 PNG 数据 |
| `POST /__tapmaker/control/refresh\|shutdown\|screenshot` | 会话控制（需令牌） |
| `POST /__tapmaker/report/logs\|milestone\|screenshot` | 页面上报（需令牌） |
| `/UrhoXRuntime.*` | 本地 Runtime 模式下的核心文件，支持 ETag 重验证 |

动态响应默认使用 `Cache-Control: no-store`。本地 Runtime 三件套使用
`private, no-cache` 与 ETag，浏览器刷新时可重验证并复用已有响应；内容寻址的
`/assets/<uuid>-<crc>.<ext>` 使用一年期 immutable 缓存。所有响应均提供
WebAssembly 运行需要的 COOP、COEP 和 CORS header。

## 测试

运行完整测试：

```bash
uv run --project skills/tapmaker-local-web/scripts \
  python -m unittest discover -s tests -v
```

测试覆盖：

- 本地目录和 entry 越界拒绝
- 内部本地命名空间稳定性
- 隐藏文件与控制目录过滤
- `.meta` UUID 与 CRC 映射
- 平台入口包装与禁用 mock
- manifest、资源与 CORS HTTP 接口
- 资源路径穿越拒绝
- revision 请求不触发文件系统扫描
- 原生文件事件触发单次刷新并通过 SSE 发布 revision
- 缺失 `.meta` 的诊断与修复后清除
- 文件修改后的 revision 与资源 URL 更新
- Runtime 下载、校验、缓存和 WASM Content-Type
- Runtime ETag/304 重验证与内容寻址资源长期缓存
- 控制面令牌鉴权、Host 校验与请求体限制
- 日志环形缓冲、里程碑与截图上报校验
- preview 会话后台启动、status/refresh/logs/screenshot/check/stop 全生命周期
- 预览页控制脚本的注入与渲染

验证 Skill 结构：

```bash
uv run --with pyyaml python \
  /path/to/skill-creator/scripts/quick_validate.py \
  skills/tapmaker-local-web
```

## 常见问题

### 端口被占用

```bash
tapmaker-local-web web \
  --code /path/to/game-content \
  --entry scripts/main.lua \
  --port 8766
```

### 修改后没有刷新

直接访问 `/__tapmaker/revision`，确认 revision 是否变化；再检查修改的文件是否位于 `--code` 目录中，以及它是否被安全过滤规则排除。

### 入口不存在或被拒绝

确认 `--entry` 是相对路径，并且从 `--code` 开始可以实际访问该文件：

```text
--code  /project
--entry scripts/main.lua
实际文件 /project/scripts/main.lua
```

### 出现官方资源 WARNING

先检查本地 manifest、UUID、CRC、`fs_path` 和具体 `/assets/...` 请求。engine-res 或 official-res 的 WARNING 应与官方远程预览做差分，不要仅凭日志关键词数量判断本地映射失败。

### 页面能打开但游戏没有启动

检查：

- entry 是否正确
- 浏览器控制台与引擎日志
- Lua 入口是否实际打印启动日志
- 必要资源是否出现在 manifest
- 是否错误地使用了 `--no-platform-mock`

## 项目结构

```text
.
├── README.md
├── LICENSE
├── examples/
│   └── demo-project/
├── tests/
└── skills/
    └── tapmaker-local-web/
        ├── SKILL.md
        ├── agents/
        │   └── openai.yaml
        ├── references/
        │   └── operations.md
        └── scripts/
            ├── pyproject.toml
            ├── uv.lock
            └── src/
                └── tapmaker_local_web/
```

## 来源与许可

许可证：[MIT](LICENSE)
