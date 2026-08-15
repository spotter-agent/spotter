<div align="center">

<h1>Spotter 安装与使用详细指南</h1>

<p>
  <a href="user-guide.md">English</a> ·
  <a href="user-guide.ko.md">한국어</a> ·
  <strong>简体中文</strong>
</p>

<p>
  面向高级用户的安装、运行时操作、配置、升级、安全移除、<br />
  故障排查与问题报告指南。
</p>

<p><a href="../README.zh-CN.md">← 返回 README</a></p>

</div>

---

> [!IMPORTANT]
> Spotter 正在积极开发中。确定性 Hook 门控现在已经可用，但语义 `VERIFY` 和 `NUDGE` 决策
> 仍只会在影子模式下记录，不会传递到实时轮次。App Server 观察和控制需要显式配置。权威的
> 当前能力边界请参阅 [Status（英文）](status.md)。

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="#3-使用-homebrew-安装"><strong>📦 使用 Homebrew 安装</strong></a><br />
      <sub>使用受支持的官方软件包安装路径。</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#4-从源码手动安装"><strong>🛠️ 手动安装</strong></a><br />
      <sub>从稳定的 Python 环境运行 Spotter。</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#5-将-spotter-连接到-codex"><strong>🔌 连接 Codex</strong></a><br />
      <sub>预览、应用并验证托管集成。</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#9-解除集成与卸载"><strong>🧹 安全移除</strong></a><br />
      <sub>区分解除集成、软件包卸载和用户数据。</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#10-故障排查"><strong>🩺 故障排查</strong></a><br />
      <sub>诊断 PATH、守护进程、配置和观察问题。</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#11-报告问题"><strong>📝 报告问题</strong></a><br />
      <sub>在不暴露敏感数据的情况下收集有效诊断。</sub>
    </td>
  </tr>
</table>

## 1. 选择安装方式

| 方式 | 适用场景 | 维护方式 |
| --- | --- | --- |
| Homebrew（推荐） | 常规 macOS 和 Linux 使用 | Homebrew 管理软件包；Spotter 只管理自己的 Codex 集成 |
| 独立 Python 环境 | 源码评估和高级/手动安装 | 你负责 Python、虚拟环境、源码更新和可执行文件路径稳定性 |
| 可编辑开发安装 | 修改 Spotter 本身的贡献者 | 在手动安装责任之外，还需管理开发依赖和仓库检查 |

除非你会主动管理当前使用的 `spotter` 和 `spotterd` 组合，否则不要让 Homebrew 与手动安装
同时出现在一个 `PATH` 中。设置过程会记录稳定的可执行文件路径；混用安装方式很容易造成 CLI
与守护进程构建不一致。

## 2. 前置条件

所有安装方式都需要：

- 在集成设置前已安装且能从 `PATH` 调用的 `codex` CLI；
- 可以在个人主目录中创建文件的用户账户；
- 使用快照、分支、回放或源码安装时所需的 Git。

手动安装还要求 Python 3.11 或更新版本。继续前请检查相关工具：

```bash
codex --version
git --version
python3 --version
```

不要用 `sudo` 运行 Spotter 设置。集成、后台服务、运行时套接字和数据都应属于当前登录用户。

## 3. 使用 Homebrew 安装

从官方 tap 安装：

```bash
brew install spotter-agent/spotter/spotter
```

确认 CLI 和守护进程来自同一个软件包边界：

```bash
command -v spotter
command -v spotterd
spotter --version
spotterd --version
```

安装软件包不会修改 Codex 配置、注册 Hook 或启动服务。这些变化只在显式设置时发生。

## 4. 从源码手动安装

请使用路径长期保持稳定的独立虚拟环境。下面的示例将源码与已安装入口点分开：

```bash
mkdir -p ~/.local/src ~/.local/share/spotter
git clone https://github.com/spotter-agent/spotter.git ~/.local/src/spotter
python3 -m venv ~/.local/share/spotter/venv
~/.local/share/spotter/venv/bin/python -m pip install --upgrade pip
~/.local/share/spotter/venv/bin/python -m pip install ~/.local/src/spotter
```

将该环境的 `bin` 目录加入 shell 的 `PATH`，或通过绝对路径调用入口点。设置前确认两个命令
位于同一目录：

```bash
~/.local/share/spotter/venv/bin/spotter --version
~/.local/share/spotter/venv/bin/spotterd --version
```

如果该环境尚未位于 `PATH`，请在执行后续示例前激活它：

```bash
source ~/.local/share/spotter/venv/bin/activate
```

Spotter 会把发现的 CLI 和守护进程路径持久写入集成配置。设置后不要移动或删除虚拟环境。
请先解除集成，或者在同一个稳定路径上重建环境并重新运行设置。

贡献者如需可编辑安装，请改用仓库内的工作流：

```bash
cd ~/.local/src/spotter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

修改项目前请阅读 [Contributing](../CONTRIBUTING.md#local-setup)。

## 5. 将 Spotter 连接到 Codex

始终先查看变更计划：

```bash
spotter setup codex --dry-run
```

然后应用托管集成并进行端到端验证：

```bash
spotter setup codex
spotter doctor
```

托管设置具备事务性和幂等性。它只修改 Spotter 所有的 Codex Hook/插件状态，保留带
fingerprint 的备份，将 `spotterd` 注册为登录用户范围的服务，验证一次合成 Hook 往返，并默认
在 `~/.spotter/integrations/codex.json` 中提交所有权清单。

如果无法或不希望注册持久用户服务，请使用 portable 模式：

```bash
spotter setup codex --portable
spotter doctor
```

Portable 模式会启动 `spotterd`，但不会注册登录时自动启动的服务。注销、重启或进程终止后，
你需要自行再次启动它：

```bash
spotter daemon start
```

完成设置后，照常使用 Codex：

```bash
codex
```

## 6. 配置

配置文件是可选的。Spotter 默认查找 `~/.spotter/spotter.toml`。设置 `SPOTTER_HOME` 会同时
移动 Spotter 的配置、数据、集成、运行时和日志根目录。也可以在设置和诊断时用 `--config`
显式指定文件。

一份保守的初始配置如下：

```toml
observation_only = true
snapshot_on_patch = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
# on_signals = true
# deliver_on_signals = true  # 需要 on_signals 和 observation_only = false
# every_steps = 25
max_per_session = 20
max_per_day = 100

[gates]
forbidden_paths = []
block_dependency_changes = false
```

通过以下命令验证并注册显式配置：

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml
```

信号驱动和定期语义审查会消耗模型令牌，并且默认关闭。请有意识地启用它们，保留每会话和每日
上限，并记住当前的审查决策只会被记录。

## 7. 操作与检查 Spotter

### 健康状态与运行时

```bash
spotter status
spotter doctor
spotter daemon status
```

`spotter status` 提供快速、非侵入式摘要；`spotter doctor` 执行更深入的合成检查。退出码含义
如下：

| 退出码 | 含义 |
| --- | --- |
| `0` | 健康 |
| `1` | 监督仍可工作但存在警告，或某个已配置能力降级 |
| `2` | 必需的集成、守护进程、存储或账本约定已损坏 |

设置后可能出现 App Server 未配置的观察/实时控制警告。这是当前已知边界：确定性的
PreToolUse Hook 执行不受影响，但完整 App Server 观察和实时控制不可用。

手动守护进程命令只控制 `spotterd`，绝不会停止或重置共享 Codex App Server：

```bash
spotter daemon start
spotter daemon restart
spotter daemon stop
```

### 已采集数据与覆盖率

```bash
spotter metrics
spotter observability
spotter analyze
```

对 `analyze`、`metrics` 或 `observability` 使用 `--session <id>`，可以把输出限制到一个已记录
会话。Reviewer 调用和已标注结果还会按 `signal`、`periodic`、`manual` 启动来源拆分，避免静默
混合 A/B 队列。请把日志和诊断视为潜在敏感信息：它们可能包含仓库路径、提示词、工具 payload 和其他
工作上下文。

检测延迟研究应同时记录回顾性的 semantic window，以及 Spotter 实际能够观察到必要证据的
observable window；不假设两个区间互相包含。每个锚点必须对应具有稳定 Trace IR event ID 的日志步骤：

```bash
spotter label-opportunity --session <id> --opportunity-id <failure-id> \
  --semantic-earliest <step> --semantic-latest <step> \
  --observable-earliest <step> --observable-latest <step> \
  --required-evidence <step> --note "需要干预的依据"
```

可重复使用 `--required-evidence`；独立双重标注时请使用 `--rater`。
`spotter metrics --session <id>` 只关联引用了全部 required evidence 的 signal，并报告区间相对
延迟以及 signal 前的 action、失败 outcome 和文件数量。尚未闭合的窗口保持为 `UNJUDGEABLE`，
不会被误报为 `NEVER`。对于 signal-driven review，它还会通过 candidate event ID 跟踪队列、
推理开始和决策，报告各阶段步数延迟，并显式区分 stale、终止前无决策和观察缺口覆盖。
对于非 stale 的 `VERIFY`/`NUDGE` 决策，它还会继续跟踪控制分发及 RPC 接受或终止结果；缺失、
stale、失败、未知和跨观察缺口的控制阶段都会保留为独立覆盖状态。
只有在精确匹配目标 thread、turn 和 connection epoch 时观察到 client message ID，已接受的 steer
才会计为 adopted。目标 turn 在缺少该证据时结束会标为 `RPC_ACCEPTED_ONLY`；接受后 stale 以及
身份或观察缺口导致的未知状态也会显式保留。

### 存储维护

快照清理默认是 dry-run。请在相关仓库内运行，或显式指定仓库：

```bash
spotter prune --repo /path/to/repository --forks
spotter prune --repo /path/to/repository --forks --apply
```

日志到期清理需要明确指定期限，并可能移除用于从旧会话分支的快照：

```bash
spotter prune --repo /path/to/repository --journals --max-age-days 30
spotter prune --repo /path/to/repository --journals --max-age-days 30 --apply
```

添加 `--apply` 前请先阅读 dry-run 输出。Spotter 会通过 Git 感知的方式清理其所有的 ref 和
worktree；不要用原始递归删除代替它。

## 8. 升级与重装

Homebrew 安装的升级方式：

```bash
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

重新运行设置会协调已安装 CLI、正在运行的守护进程、生成的 Hook 路径、构建标识和集成版本。

独立源码环境的升级方式：

```bash
git -C ~/.local/src/spotter pull --ff-only
~/.local/share/spotter/venv/bin/python -m pip install --upgrade ~/.local/src/spotter
source ~/.local/share/spotter/venv/bin/activate
spotter setup codex
spotter doctor
```

如果升级中断，不要先手动编辑生成的 Hook。运行 `spotter status`，然后重新执行 setup 和
doctor。设置过程会利用保留的所有权状态进行协调，而不会重复添加 Hook。

## 9. 解除集成与卸载

如需保留 Spotter 和数据，只移除 Codex 集成：

```bash
spotter teardown codex
```

如需完整卸载 Homebrew 安装：

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

对于手动 Python 安装，请在命令仍然存在时先解除集成，再从同一环境卸载发行包：

```bash
spotter teardown codex
spotter daemon stop
~/.local/share/spotter/venv/bin/python -m pip uninstall spotter-agent
```

确认这些准确路径专用于 Spotter 后，可以用常用文件管理工具删除虚拟环境和源码检出。

普通卸载不会删除 `~/.spotter` 或 `SPOTTER_HOME` 中的用户数据。仓库感知的 `spotter purge`
命令**尚未实现**。Spotter 所有的 Git ref 和分离 worktree 也可能存在于各仓库中，因此只删除
主目录下的数据并不等于完整 purge。请用 `spotter prune` 执行受支持的 Git 感知清理，保留
不确定的数据，并通过 [#89](https://github.com/spotter-agent/spotter/issues/89) 跟踪 purge 支持。

如果在 teardown 前就卸载了软件包，生成的 Hook 会按设计采用 fail-open 行为。使用相同方式
重新安装软件包，运行 `spotter teardown codex`，然后再次卸载，即可安全清理已记录的所有
集成。

## 10. 故障排查

<details>
<summary><strong>找不到 <code>spotter</code> 或 <code>spotterd</code></strong></summary>

```bash
command -v spotter
command -v spotterd
```

对于 Homebrew，请确认 Formula 已安装且 Homebrew 的 `bin` 目录位于 `PATH`。对于手动安装，
请激活目标虚拟环境。如果两个命令指向不同的安装根目录，请修正 `PATH`，然后重新运行
`spotter setup codex` 和 `spotter doctor`。

</details>

<details>
<summary><strong>设置找不到 Codex</strong></summary>

```bash
command -v codex
codex --version
spotter setup codex --dry-run
```

请在运行设置的同一登录环境中安装 Codex，或将它加入 `PATH`。如果使用自定义 `CODEX_HOME`，
请在 setup、doctor 和日常 Codex 会话中保持一致。

</details>

<details>
<summary><strong>守护进程不可用或构建不匹配</strong></summary>

```bash
spotter daemon status
spotter daemon restart
spotter setup codex
spotter doctor
```

升级后重新运行 setup 是受支持的协调步骤。在 doctor 输出明确指出已注册服务本身有问题之前，
不要手动编辑 `launchd` 或 `systemd --user` 定义。

</details>

<details>
<summary><strong>Doctor 报告观察或实时控制不可用</strong></summary>

目前不会自动选择 App Server 端点。如果 Hook 执行显示可用，确定性门控仍然工作；App Server
观察和实时 `VERIFY`/`NUDGE` 则不可用。将这个已知产品边界当作安装错误前，请先检查
[Status（英文）](status.md)。

</details>

<details>
<summary><strong>配置解析失败或似乎被忽略</strong></summary>

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml --dry-run
```

确认 `[main_agent]` 存在，并且 `adapter = "codex"` 是非空字符串。Setup 会把选定配置路径写入
集成清单。如果已记录的配置变得不可用，Hook 会在 fail-open 边界内使用安全默认值，并将问题
输出到 stderr。

</details>

<details>
<summary><strong>没有显示会话或最近观察</strong></summary>

设置后运行一次普通 Codex 会话，然后检查：

```bash
spotter status
spotter doctor
spotter observability
```

检查 Hook 所有权结果、最后观察时间，以及 Codex 会话是否使用了 setup 检查时相同的
`CODEX_HOME`。

</details>

<details>
<summary><strong>存储或权限检查失败</strong></summary>

默认情况下，可变状态位于 `~/.spotter`，其中包括 `logs/`、`runtime/`、`sessions/` 和
`integrations/`。确认当前用户拥有并可写入所配置的根目录。不要用宽泛的递归命令修改所有权；
请检查 doctor 显示的准确失败路径。

</details>

<details>
<summary><strong>未执行 teardown 就卸载了 Spotter</strong></summary>

Codex 应继续运行，因为生成的 Hook 会采用 fail-open 行为。要清理 Spotter 所有的集成，请重新
安装 Spotter，运行 `spotter doctor`，再运行 `spotter teardown codex`，最后再次卸载。

</details>

<p align="right"><a href="#spotter-安装与使用详细指南">返回顶部 ↑</a></p>

## 11. 报告问题

打开 [GitHub 议题选择器](https://github.com/spotter-agent/spotter/issues/new/choose)，根据请求选择
错误、功能、文档、架构、实验或维护表单。

一份有用的错误报告应包含：

- 实际发生的情况、预期行为及影响；
- 能稳定重现问题的最小步骤；
- Homebrew 或手动安装方式；
- 相关的 Spotter CLI/守护进程版本、Codex 版本、操作系统、架构和 Python 版本；
- 失败的准确命令与退出码；
- 相关的 `status`、`doctor` 和 `daemon status` 输出行；
- 问题是否在 setup、升级、配置变更或重装后开始出现。

通过以下命令收集基础诊断：

```bash
spotter --version
spotterd --version
codex --version
spotter status
spotter doctor
spotter daemon status
```

请删去令牌、凭据、私有仓库名称和路径、提示词、源代码及个人信息。未经检查，不要附上原始
日志、完整配置文件或完整运行日志。对于涉及安全或可被利用的问题，不要在公开议题中发布
细节；请在仓库 Security 页面检查是否提供私密报告渠道。

## 12. 相关文档

- [Status（英文）](status.md) — 已实现能力与实验性能力的权威边界
- [Lifecycle](lifecycle.md) — 软件包、服务、集成、恢复和移除的完整约定
- [配置参考](../spotter.example.toml) — 当前已文档化的全部配置项
- [Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) — 软件包生命周期证据
- [Contributing](../CONTRIBUTING.md) — 源码设置与贡献流程
