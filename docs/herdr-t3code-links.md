# herdr 与 T3 Code 学习资料

> 链接已于 2026-11-24 逐一核实可访问。

## herdr（终端运行时，Rust，Apache-2.0）

- 仓库：https://github.com/herdrdev/herdr （另有中文 README：README.zh-CN.md）
- 文档首页：https://herdr.dev/docs/
- 先读：
  - https://herdr.dev/docs/quick-start/ —— 装好并第一次跑起来，学会断开和重新连上
  - https://herdr.dev/docs/concepts/ —— workspace / tab / pane / agent 四层，以及 working / blocked / idle 状态
  - https://herdr.dev/docs/session-state/ —— 断开、服务重启、升级时哪些东西能保住
  - https://herdr.dev/docs/agent-skill/ —— 让 Claude 通过 herdr 命令开 pane、派子 agent、等待完成
  - https://herdr.dev/docs/socket-api/ —— 脚本接入用的控制接口
- 安装：`brew install herdr` 或 `curl -fsSL https://herdr.dev/install.sh | sh`（出处：仓库 README）
- 无界面内核 herdr-core：https://github.com/herdrdev/herdr-core （命令说明见 docs/commands.md）

学习顺序：quick-start 开两个 pane 分别跑 Claude 和 Codex → 读 concepts、session-state → `herdr integration install claude/codex` 并装 agent skill → 读 socket-api 和 herdr-core。

## T3 Code（多 agent 图形控制面，TypeScript，MIT）

- 仓库：https://github.com/pingdotgg/t3code ，官网：https://t3.codes
- 文档首页：https://github.com/pingdotgg/t3code/blob/main/docs/README.md
- 先读：
  - docs/user/install.md —— 安装和各 agent 的登录配置
  - docs/user/permission-modes.md —— 四种权限模式（注意新线程默认是 Full access）
  - docs/user/source-control.md —— 每个线程独立 worktree、审 diff、提交、开 PR
  - docs/user/outside-agents.md —— 让终端里的 Claude Code 通过 MCP 开线程、读线程
  - docs/internals/01_architecture.md —— 服务端、客户端、provider 三层结构
- 多模型相关：
  - docs/user/workflows.md —— 用 TypeScript 写可持久化的多阶段流程（Labs 功能，默认关闭）
  - docs/user/portable-handoffs.md —— 切换模型时如何带上下文（默认上限 16k token）
  - docs/user/remote-access.md#link-another-environment —— `delegate_task` 派任务到另一台机器
  - docs/user/thread-sidebar.md#inspect-agent-work —— 跟踪子 agent
- 安装：`brew install --cask t3-code`、`curl -fsSL https://t3.codes/install.sh | sh` 或 `npx t3@latest`（出处：仓库 README）

（以上 docs/ 路径均以 https://github.com/pingdotgg/t3code/blob/main/ 为前缀）

学习顺序：装好后开一个 Claude 线程、一个 Codex 线程 → 读权限模式和 source-control，跑通"新 worktree → 审 diff → 开 PR" → 读 remote-access、outside-agents，连上手机 → 读 portable-handoffs、workflows 和 internals。

## 三者怎么并行使用

| | multi-model-team | herdr | T3 Code |
|---|---|---|---|
| 定位 | 协作流程（skill + 三个脚本） | 终端运行时 | 图形控制面 |
| 管什么 | 谁做什么、怎么交接、审查、验收、留档 | 进程常驻、pane 状态、多台机器 | 线程、权限审批、diff/PR、手机端 |
| 不管什么 | 进程常驻、界面 | 分工和质量把关 | 结构化的分工和审查纪律 |
| 最适合 | 一个需求要多个模型配合交付 | 长时间并行跑、SSH 断线后继续 | 随时随地审批和审阅 |
| 怎么配合 | 作为最上层的流程 | 当它的宿主终端 | 当它的前端界面 |

组合示例：
- herdr 常驻几个 pane，其中一个 Claude pane 里跑 multi-model-team，其他 pane 跑独立任务；手机上通过 SSH 看状态（herdr 没有原生手机 App）。
- 在 T3 Code 里开一个 Claude 线程调用 multi-model-team，用 T3 的 diff 面板审 Codex 的产出，在手机上批准。

注意：
1. 同一个目录不要让多个 agent 同时改，各用各的 worktree；`codex-worker` 的工作树不要和 T3 线程的 worktree 混用。
2. 三者用的都是你自己的订阅额度，并行越多消耗越快。
3. T3 新线程默认 Full access，跑 multi-model-team 时建议改成 Supervised 或 Auto，并保留本地 git hook。
4. 派活路径只留一条：T3 自带"Claude 把任务交给 Codex"，和 `codex-worker` 功能重叠，选一个，否则 runs 目录的记录不完整。
