# multi-model-team 设计说明

本文说明 multi-model-team 的设计目标、架构和关键取舍。使用方法见 [README](../README.md)。

## 目标

一个独立的 Claude Code skill：Claude 当总指挥，负责理解需求、拆解、派活、合并、验收；Gemini 当军师（独立方案、文案与交互顾问，只读）；Codex 负责接口已定的成块代码和上线前审查。人只在提需求和拍板时出现。

## 关键决定

| 事项 | 结论与理由 |
|---|---|
| 形态 | 独立 skill，只在用户手动输入 `/multi-model-team` 时运行，不写进全局 CLAUDE.md，避免影响日常会话。 |
| Gemini 接入 | 官方 Antigravity CLI `agy`，走个人订阅；单次调用关闭自动扣费（`useG1Credits: false`）。 |
| 只读保证 | 单次生效的 deny 规则（禁写文件、禁执行命令）+ 沙箱 + 调用前后目录指纹比对；可选 `--isolate` 在临时副本中运行。 |
| Codex 工作台 | 派活时自动建 git worktree（独立分支 `codex/<名字>`），Claude 先装依赖再派活，交回后由 Claude 读 diff、跑测试、合并。 |
| 审查 | 用 `codex exec`（只读沙箱）+ `--output-schema` 取结构化结果；不用 `codex exec review`，因为它不允许自定义要求与范围参数同时使用，且忽略 output schema。 |
| 记录目录 | `~/.multi-model-team/runs/<项目名>-<日期>-<任务>/`，不放项目里：避免 Gemini 读到 Claude 的草稿，也避免弄脏仓库。 |
| 关卡位置 | 本地 git pre-push 钩子；本地钩子可被 `--no-verify` 绕过，强制保证交给 CI 与分支保护。 |

## 架构

| 层 | 职责 | 实现 |
|---|---|---|
| 调度层 | 理解需求、拆解、按规则派活、合并、验收 | Claude Code 主会话 + `skill/multi-model-team/SKILL.md` |
| 执行层 | 安全地调用其他模型 | `agy-ask`、`codex-worker`、`codex-review`、`codex-worktree` |
| 关卡层 | 推送前把关、把纠错变成检查 | `review-gate`、`guard-check` |

```
bin/        六个命令入口（薄壳，调用 lib/）
lib/        multi_model_team/ 实现
prompts/    给 Gemini / Codex 的固定要求
schemas/    review.schema.json（审查结果结构）
config/     models.toml：角色 → 模型 / 档位 / 超时 / CLI 路径
skill/      multi-model-team/SKILL.md：分工、硬规则、流程
scripts/    install.sh
tests/      假 CLI（fake_codex / fake_agy）+ 单元测试
```

## 调度流程（状态机）

```
理解分级 → 双方案（Claude + Gemini 各自独立）→ 合并 → 🔴卡点A（人确认方案）
       → 派活执行（Codex 工作台 / Claude 自己 / Gemini 文案顾问）
       → Claude 读 diff、跑测试 → Codex 审查（最多两轮）→ 🔴卡点B（人确认交付）
```

所有状态落在记录目录的文件里（`task.md`、`plan.md`、`state.md`、`dispatch/`、`review-N/` 等），用结构化结果（`result.json`、退出码）决定走向。

### 派活规则

- 几十行以内的改动、修审查问题、要和页面来回对接口的，Claude 自己做：派一次活的沟通成本可能比活本身还大。
- 接口已定死、和页面无关的独立模块交给 Codex；Claude 定接口和验收标准。
- 大方案让 Gemini 独立出一份：只给用户原话和必要背景，不给 Claude 的草稿和倾向。
- 界面文案和交互由 Claude 先做一版，再请 Gemini 当顾问改，Claude 核实后采纳。
- 任何模型的汇报都"不算数"：以实际执行的命令及退出码、diff、Claude 自己跑的测试为准。

## 审查规则

- 挡上线的问题必须写出触发路径，否则自动降为建议。
- 改动文件路径命中钱、权限、对外消息等关键词时自动用深审档位。
- 最多两轮；第二轮仍有挡上线问题时交给人决定（退出码 4）。
- 审查通过会在 `.git/multi-model-team/review-pass/` 留下"已审"标记，记录被审代码的 tree 和审查起点；推送时，全量审查的标记总是有效，差异审查的标记只在审查起点和推送起点是同一个提交时有效；审查期间代码有变化就不留标记，同一份代码之后被判为挡上线，标记随即作废。关卡始终以 git 实际要推送的提交为准，检查过程中检出或工作区变了就拦住推送；日志和状态里的远端地址会去掉令牌。

## 推送前关卡

`git push` 时依次运行：项目自检 → 守卫检查 → Codex 审查，前一道不过就不跑后面。

- 只有挡上线问题才拦推送；建议项只打印。
- 有未提交改动时拒绝推送，保证检查的就是要推的代码。
- 审查跑不起来（CLI 故障、超时）时拦住推送，提示人工放行：`MULTI_MODEL_TEAM_ALLOW_PUSH="原因" git push`，原因写入日志。
- 项目已设置 `core.hooksPath`（如 husky）时不安装，提示手动接入。

## 纠错变检查

用户纠正一次、审查挡回一条，就在修它的同一次改动里拦住这一类错误，优先级：

1. 能机械判断的，写成测试或守卫规则（报错里写清楚怎么改）；
2. 能改结构让错误写法写不出来的，改结构；
3. 都做不到，才写进项目文档的文字规则，并写明原因。

守卫规则写在 `.multi-model-team/project.toml` 的 `[[guards]]`；已有违规记成欠账（`guard-baseline.json`），欠账只许减少，新增违规一律报错。欠账按正则的实际匹配次数计（同一行多一处违规也算新增）；旧版按行计的欠账文件按原口径兼容，`--update-baseline` 时换算。`--update-baseline` 只能登记新规则的欠账或下调已有欠账，不能调高。

## 测试

- `tests/` 用假的 `codex` / `agy` CLI 覆盖参数、权限、超时、结果判定、工作台、守卫欠账、钩子安装和推送到本地模拟远程仓库的各种情形，不调用真模型。
- CI：Ubuntu / macOS × Python 3.11–3.14 跑全部用例；ruff 检查与格式；覆盖率门槛；shellcheck 检查安装脚本。
- 真模型验证在开发阶段手工进行（冒烟、真实小任务演练、本地模拟远程推送），不进 CI。

## 风险

- 各家 CLI 迭代很快，调用细节集中在 `lib/` 与 `config/models.toml`，便于跟着改。
- 跨模型调用消耗各家额度，派活门槛要守住，小活不派。

## 以后可能做

- [herdr](https://github.com/herdrdev/herdr)：需要同时盯多个 agent、远程看进度时，用作运行时。
- [T3 Code](https://github.com/pingdotgg/t3code)：需要图形界面、手机审批时再试。
