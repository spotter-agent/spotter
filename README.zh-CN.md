<div align="center">

<p>
  <a href="README.md">English</a> ·
  <a href="README.ko.md">한국어</a> ·
  <strong>简体中文</strong>
</p>

<h1>Spotter</h1>

<picture>
  <img alt="Spotter" src="docs/assets/main-ts.png" width="250" />
</picture>

<h3>在错误的编码智能体轨迹造成高昂代价之前发现它们。</h3>

<p>
  Spotter 是一款面向编码智能体的本地运行时监督工具。<br />
  它不仅查看最终 diff，还观察工作过程，从而尽早发现代价高昂的偏离。
</p>

<p>
  <code>本地优先</code> · <code>有界门控</code> · <code>轨迹感知</code>
</p>

<p>
  <a href="#安装"><strong>安装</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#快速开始"><strong>快速开始</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/user-guide.zh-CN.md"><strong>详细指南</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/status.md">当前状态（英文）</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/README.md">完整文档</a>
</p>

</div>

---

## 为什么需要 Spotter

编码智能体很少因为一个明显的步骤而失败。一个薄弱的假设可能影响后续的搜索、编辑和测试；
反复出现的局部判断，最终会把原本可以恢复的错误变成时间、令牌和仓库改动的浪费。

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>🔭 轨迹感知</strong><br />
      <sub>不仅查看最终 diff，还观察决策、证据、编辑和验证的过程。</sub>
    </td>
    <td width="33%" valign="top">
      <strong>⚡ 有界安全检查</strong><br />
      <sub>在高风险工具调用前，通过本地快速执行确定性检查。</sub>
    </td>
    <td width="33%" valign="top">
      <strong>🩺 明确的降级状态</strong><br />
      <sub>在不阻塞 Codex 的前提下，让观察或控制问题可被诊断。</sub>
    </td>
  </tr>
</table>

Spotter 独立观察正在进行的工作轨迹，帮助回答：

- 智能体是否在没有获得新信息的情况下重复失败或执行等价操作？
- 修改是否正在超出请求的范围？
- 发生重要修改后，是否缺少相应验证？
- 智能体是否仍依据已被新证据削弱的假设行动？
- 是否即将违反确定性的安全规则？

目标不是产生更多警报，而是减少第一次实质性偏离之后的无效工作。

## Spotter 目前能做什么

当前运行时可以：

- 将 Codex Hook 和已配置的 App Server 事件收集到持久的轨迹日志中；
- 维护由守护进程持有的线程、轮次、证据、进度和检测信号实时状态；
- 在高风险工具调用前执行有严格时限的确定性门控；
- 检测循环、停滞的探索、范围扩张、缺失验证和过时假设的候选信号；
- 以影子模式运行可选的语义审查，并提供单独的实时 advisory 启用选项；
- 通过 `spotter status` 和 `spotter doctor` 提供健康状态与集成诊断；
- 保留基于 Git 的快照和回放材料，用于恢复与分析。

> [!IMPORTANT]
> Spotter 正在积极开发中。确定性门控已经生效，但语义 `VERIFY` 和 `NUDGE` 决策默认仅以
> 影子模式运行，只有显式启用 active mode 后才可能传递。实时 advisory 的收益和任务归属安全性
> 尚未得到证实。部分 App Server 观察与控制能力仍需要显式配置。准确的当前边界请参阅
> [当前状态（英文）](docs/status.md)。

Codex 是目前主要的独立集成目标。

## 工作原理

```text
Codex
  ├─ Hooks ────────────────► 有严格时限的确定性门控
  └─ App Server 事件 ──────► 配置后提供观察与控制
                                   │
                                   ▼
                                spotterd
                                   │
                 日志 · 实时状态 · 信号 · 影子审查
```

确定性门控始终在限定时间内执行，而较慢的语义审查位于同步工具执行路径之外。如果观察或控制
能力降级，诊断会明确显示；生成的 Hook 会采用 fail-open 行为，不阻碍 Codex 的正常使用。

## 安装

支持通过官方 Homebrew tap 安装：

```bash
brew install spotter-agent/spotter/spotter
```

验证两个入口点均已安装：

```bash
spotter --version
spotterd --version
```

安装软件包只会安装 CLI、守护进程和 Hook 桥接器，**不会**修改 Codex 配置或注册集成。

如需从源码或开发检出进行安装，请参阅
[CONTRIBUTING.md](CONTRIBUTING.md#local-setup)。

## 快速开始

确认 `codex` CLI 已安装并可通过 `PATH` 调用，然后预览并应用托管集成：

```bash
spotter setup codex --dry-run
spotter setup codex
spotter doctor
```

设置过程具备事务性和幂等性。它会准确记录 Spotter 所有的 Hook 和服务状态，因此后续修复或
解除集成时无需猜测用户所有的配置。

完成后，照常使用 Codex：

```bash
codex
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `spotter status` | 显示集成、守护进程、能力和存储健康状态 |
| `spotter doctor` | 运行合成健康检查并输出可执行的诊断建议 |
| `spotter daemon status` | 检查软件包安装的 `spotterd` 进程及构建标识 |
| `spotter metrics` | 汇总已收集的运行时和评估指标 |
| `spotter observability` | 检查可用的轨迹来源和规范化事件 |
| `spotter --help` | 显示完整命令列表 |

配置文件是可选的。如需自定义门控、存储、快照或审查预算，请参考
[spotter.example.toml](spotter.example.toml)。信号驱动的语义审查会消耗模型令牌，并且默认
关闭；请仅在确有需要时启用，并保留已有的每会话和每日限额。

## 升级

升级 Formula 后再次运行设置，让 Spotter 协调已安装构建、正在运行的守护进程和集成版本：

```bash
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

持久 Hook 和服务引用使用稳定的软件包入口点，而不是带版本号的 Homebrew Cellar 路径。
Spotter 会检测仍在运行的旧版守护进程，不会假设它与新安装的 CLI 相同。

## 解除集成或卸载

如需保留 Spotter，只移除 Codex 集成：

```bash
spotter teardown codex
```

如需完整卸载：

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

Homebrew 卸载会移除软件包所有的可执行文件，并停止软件包运行时。它有意保留单独管理的
`~/.spotter` 用户数据。如果未先解除集成就卸载，残留集成会按设计采用 fail-open 行为，并可
在重新安装后修复。

如果升级、恢复、迁移、解除集成或数据删除超出上述常规路径，请先参阅
[Lifecycle](docs/lifecycle.md)。

## 运行保障

<details>
<summary><strong>查看安全与所有权保障</strong></summary>

- `brew install` 和 `brew upgrade` 不会静默修改 Codex 配置。
- `spotter setup codex` 和 `spotter teardown codex` 只修改其明确所有的集成状态。
- 生成的 Hook 使用稳定的可执行文件路径，并在 Spotter 不可用时采用 fail-open 行为。
- Spotter 不会停止其无法证明拥有的共享 Codex App Server。
- 卸载与用户数据清除是两个独立操作。
- `status` 和 `doctor` 会明确显示观察或控制能力的降级。

这些约定由快速夹具测试和真实的 macOS Homebrew 安装 → 在线升级 → 卸载 → 重装生命周期
冒烟测试覆盖。证据和复现方法请参阅
[Homebrew lifecycle smoke](docs/homebrew-lifecycle-smoke.md)。

</details>

## 文档

| 如果你想了解…… | 请阅读 |
| --- | --- |
| 当前可用功能与仍处于实验阶段的功能 | [当前状态（英文）](docs/status.md) |
| 安装、配置、运行、故障排查或卸载 | [安装与使用详细指南](docs/user-guide.zh-CN.md) |
| 软件包与集成的完整约定 | [Lifecycle](docs/lifecycle.md) |
| 产品理念与干预模型 | [Concept](docs/concept.md) |
| 运行时边界与持久状态 | [Architecture](docs/architecture.md) |
| 后续工作与证据门槛 | [Roadmap](docs/roadmap.md) |
| 实验、假设和证据 | [Research](docs/research.md) |
| 构建 Spotter 或参与贡献 | [Contributing](CONTRIBUTING.md) |
| 浏览所有项目文档 | [文档索引](docs/README.md) |

---

<p align="center">
  由 <a href="https://github.com/Bogyie">@bogyie / Bogyoeng Kim</a> 和
  <a href="https://github.com/YoungJinJung">@zerone / Youngjin Jung</a> 维护。<br />
  采用 <a href="LICENSE">MIT 许可证</a>发布。
</p>
