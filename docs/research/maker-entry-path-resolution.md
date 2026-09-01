# TapTap Maker entry 与资源目录路径解析

调研日期：2026-09-01

## 结论

本次修正前，本地预览的多目录实现没有完全对齐 Maker：Maker 将 `scriptsPath` 和
`entry` 作为两个独立概念。标准单机项目存在 `scripts/main.lua` 且调用者没有显式指定入口时，
官方 Maker MCP 传给远端构建的是：

```json
{
  "scriptsPath": "scripts",
  "entry": "main.lua"
}
```

因此 Maker 的运行时入口是 `main.lua`，不是 `scripts/main.lua`。`scripts/` 是源码目录，
不会成为该目录中文件的运行时 `fs_path` 前缀。官方 Maker MCP 的 tool schema 也明确把
`entry` 定义为“相对于 `scriptsPath` 的 Lua 文件”。

同理，标准 `asset_dirs=["../assets", "../scripts"]` 是多个并列的资源根；每个根中的内容
分别挂到同一个运行时资源命名空间，而不是把根目录名保留进资源路径。一个已由 Maker 发布的
项目提供了可直接核验的对应关系：

| 本地源文件 | Maker manifest `fs_path` |
| --- | --- |
| `scripts/main.lua` | `main.lua` |
| `scripts/theme_studio/ThemeState.lua` | `theme_studio/ThemeState.lua` |
| `assets/Fonts/Inter-Regular.ttf` | `Fonts/Inter-Regular.ttf` |

该项目的 `.project/project.json` 记录的入口也是 `main.lua`，线上 manifest 的顶层 `entry`
同样为 `main.lua`。

## 一手证据

1. TapTap 官方 `instant-games-open-mcp` 仓库的 Maker MCP schema 将 `entry` 描述为相对于
   `scriptsPath` 的路径；默认探测说明也写明，本地存在 `scripts/main.lua` 时发送
   `entry="main.lua"` 和 `scriptsPath="scripts"`：
   [官方 tool schema](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/src/maker/server/mcp.ts#L300-L310)。

2. 同一官方实现的 `createBuildArgs` 代码确实只有在未提供任何入口覆盖并检测到
   `<projectRoot>/scripts/main.lua` 时，才注入 `entry = "main.lua"` 和
   `scriptsPath = "scripts"`；显式 `entry` 或 `scriptsPath` 会原样优先：
   [官方构建参数实现](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/src/maker/server/mcp.ts#L3389-L3421)，
   [官方回归测试](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/src/__tests__/makerBuildLocalChanges.test.ts#L773-L830)。

3. TapTap 官方 Maker 开发文档给出相同默认行为，并说明多人入口写入
   `project.json` 的 `entry@client` / `entry@server`：
   [官方 Maker 文档](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/docs/MAKER.md#L904-L911)。

4. 官方当前项目健康检查要求 `build.generate_fs_path=true`，并要求标准
   `build.asset_dirs` 只包含 `../assets` 和 `../scripts`。这意味着“三个资源根”不是当前
   Maker MCP 承认的标准项目配置；即使底层构建器可能有数组能力，本地预览也不应把三目录
   模式宣称为 Maker 标准行为：
   [官方健康检查实现](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/src/maker/projectSettings.ts#L700-L725)，
   [官方文档中的标准默认值](https://github.com/taptap/instant-games-open-mcp/blob/22d7f5d4c74adb1f037afdc504f1951e1481c4f1/docs/MAKER.md#L940-L950)。

5. 公开 Maker 项目的源配置与 Maker 托管构建产物提供了端到端实证。源配置使用
   `asset_dirs=["../assets","../scripts"]` 且 `entry="main.lua"`；源文件位于
   `scripts/main.lua`、`scripts/theme_studio/...` 和 `assets/Fonts/...`，而 Maker 线上
   manifest 分别输出 `main.lua`、`theme_studio/...` 与 `Fonts/...`：
   [项目 settings.json](https://github.com/kirozeng/tapmaker-ui-theme-studio/blob/d144215c082d00c01af0ac55ef55c7acbcaa0230/.project/settings.json)，
   [项目 project.json](https://github.com/kirozeng/tapmaker-ui-theme-studio/blob/d144215c082d00c01af0ac55ef55c7acbcaa0230/.project/project.json)，
   [项目 scripts 目录](https://github.com/kirozeng/tapmaker-ui-theme-studio/tree/d144215c082d00c01af0ac55ef55c7acbcaa0230/scripts)，
   [项目 assets 目录](https://github.com/kirozeng/tapmaker-ui-theme-studio/tree/d144215c082d00c01af0ac55ef55c7acbcaa0230/assets)，
   [Maker 托管 manifest](https://adba8d65-92f1-400e-9ca7-526344b350a8.games.tapapps.cn/1.0.0/manifest-72cc4fce.json)。

线上 manifest 是 2026-06-12 生成的 build 96；它的顶层 `entry` 是 `main.lua`。

## 对本项目的具体影响

### `direct_project` mounts

每一个传入的资源目录都应当是一个独立运行时根。对于：

```text
/project/assets
/project/scripts
/project/config
```

mount 不能保留 `assets/`、`scripts/`、`config/` 目录名作为运行时前缀；应该把各目录内容
都挂入同一个目标命名空间。例如：

```text
/project/scripts/main.lua -> main.lua
/project/assets/images/hero.png -> images/hero.png
/project/config/game.json -> game.json
```

这会自然产生 Maker 相同的冲突约束：如果两个资源根都含 `common/foo.json`，最终
`fs_path` 相同，必须明确报冲突，不能靠保留根目录名来消除冲突。

单目录模式需要区分“项目根”与“资源根”：

- `--code /project` 表示项目根时，若直接扫描整个项目，`scripts/main.lua` 会保留
  `scripts/`，这不是标准 Maker 的两资源根行为。
- 要严格对齐 Maker，应根据 `.project/settings.json` 的 `build.asset_dirs` 建立多个资源根；
  无配置的标准兜底是 `/project/assets` 和 `/project/scripts`。
- 如果保留“任意单目录直接挂载”的兼容能力，该目录应被视为一个资源根，其内容相对于
  该目录映射到运行时根。

### manifest `fs_path`

`fs_path` 应相对于“命中该文件的资源根”，而不是相对于所有 `--code` 参数的共同父目录。
因此标准双目录输入下：

```text
scripts/main.lua       -> fs_path: main.lua
assets/images/a.png    -> fs_path: images/a.png
```

当前将它们生成为 `scripts/main.lua` 与 `assets/images/a.png` 的行为应修正。

`.meta` 查找仍应针对真实源文件的相邻路径，UUID/CRC/资源下载名逻辑不需要因为
`fs_path` 去前缀而改变；但 UUID 的无 meta 后备键应继续以最终虚拟资源身份稳定计算，且必须
避免不同资源根同名文件静默覆盖。

### `--entry` 语义

若 CLI 要复刻 Maker 参数，最清晰的契约是拆为：

```text
--scripts-path scripts
--entry main.lua
```

其中 `--scripts-path` 相对于项目工作区，`--entry` 相对于 `--scripts-path`。

如果为了现有 UX 继续接受 `--entry scripts/main.lua` 作为“源码定位路径”，内部也必须在解析出
所属资源根 `/project/scripts` 后归一化为运行时入口 `main.lua`，并在 URL、deployment entry、
manifest 顶层 `entry` 和入口匹配逻辑中统一使用 `main.lua`。不应把
`scripts/main.lua` 直接传给播放器。

建议兼容顺序：

1. 多个 `--code` 都视为资源根；先用项目相对路径或资源根相对路径定位源入口。
2. 定位到唯一源文件后，以该文件相对于所属资源根的路径计算运行时 `entry`。
3. 若 `--entry` 同时能命中多个资源根，报歧义；若不能命中任何根，报不存在或越界。
4. URL `entry=`、manifest `entry`、`Deployment.entry` 与目标文件 `fs_path` 必须完全一致。

### 三个及更多目录

本地工具可以把三个及更多目录作为兼容/实验能力支持，但映射规则仍应与 Maker 的多资源根模型
一致：每个目录内容都去掉自身根前缀后合入统一命名空间，并进行冲突检测。

不过，当前官方 Maker MCP 的健康检查会拒绝标准配置之外的第三个 `asset_dir`。因此文档应明确：

- `assets + scripts` 是当前官方标准。
- 3+ `--code` 是本地预览的扩展兼容能力，不代表该项目能通过当前 Maker MCP 的远端构建检查。
- 如果未来官方 health check 或 schema 放宽，应再以对应版本的一手实现更新此结论。

## 最小对齐验收

至少补充以下回归断言：

```text
source scripts/main.lua              => fs_path main.lua
source scripts/lib/foo.lua           => fs_path lib/foo.lua
source assets/images/hero.png        => fs_path images/hero.png
manifest.entry                       => main.lua
preview URL entry                    => main.lua
two roots containing same relative path => explicit collision error
```

并保留显式自定义脚本根场景：`scriptsPath="custom"`、`entry="boot.lua"` 应映射
`/project/custom/boot.lua -> fs_path boot.lua -> manifest.entry boot.lua`。
