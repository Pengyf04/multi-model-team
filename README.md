# multi-model-team

[![CI](https://github.com/Pengyf04/multi-model-team/actions/workflows/ci.yml/badge.svg)](https://github.com/Pengyf04/multi-model-team/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

让 Claude、Gemini、Codex 在同一个开发任务里分工协作的 Claude Code skill 和配套命令行工具。

- **Claude（Claude Code 主会话）当总指挥**：理解需求、拆解、派活、合并、验收、用人话汇报。
- **Gemini（通过官方 Antigravity CLI `agy`）当军师**：独立出一份方案、给界面文案和交互提意见，全程只读。
- **Codex（通过 Codex CLI）干活和把关**：在独立的 git 工作台里写接口已定的成块代码，上线前做结构化代码审查。

人只在两处出现：提需求、拍板。

## 前提条件

| 依赖 | 说明 |
|---|---|
| macOS 或 Linux | Windows 未测试 |
| Python 3.11+ | 只用标准库，无需安装第三方包 |
| git | 工作台、审查、推送前关卡都基于 git |
| [Claude Code](https://claude.com/claude-code) | 运行 `/multi-model-team` skill |
| [Codex CLI](https://github.com/openai/codex) | 已登录（`codex login`），用于 `codex-worker` / `codex-review` |
| Antigravity CLI `agy` | 已登录 Google 账号，用于 `agy-ask` |

只用推送前关卡和守卫（`review-gate`、`guard-check`）时，不需要 `agy`。

## 安装

```bash
git clone https://github.com/Pengyf04/multi-model-team.git
cd multi-model-team
scripts/install.sh
```

`install.sh` 会：

- 把六个命令软链到 `~/.local/bin`（请确保它在 `PATH` 里）；
- 把 skill 软链到 `~/.claude/skills/multi-model-team`，可用 `--skills-dir <目录>` 追加其他 skills 目录；
- 检查 Python、`codex`、`agy` 是否可用。

可重复运行；目标位置已有别人的文件时会停下，不会覆盖。

模型名、推理档位、超时、CLI 路径都在 [`config/models.toml`](config/models.toml) 里改。`codex` / `agy` 的配置路径不存在时，会自动回退到 `PATH` 里找同名命令。也可以用环境变量 `MULTI_MODEL_TEAM_CONFIG` 指向另一份配置。

## 用法

在 Claude Code 里输入：

```
/multi-model-team 给订单服务加优惠券功能，金额按分计算
```

skill 只在手动输入时运行，不会被自动触发。流程：

1. **理解与分级**：小活 Claude 自己做，不派。
2. **双方案**：Claude 出一份；Gemini 只拿到用户原话和必要背景，独立出一份；Claude 合并，写明采纳与不采纳的理由。
3. 🔴 **卡点 A**：用户确认方案。
4. **派活执行**：接口定死、和页面无关的模块交给 Codex，在独立工作台里做；前端、胶水代码 Claude 自己做；界面文案请 Gemini 当顾问。
5. **收货合并**：Claude 读 diff、自己跑测试，不信任何模型的口头汇报。
6. **审查**：Codex 审查，最多两轮；挡上线的问题必须写出触发路径。
7. 🔴 **卡点 B**：用户确认后再提交、推送。

每个任务的过程记录放在 `~/.multi-model-team/runs/<项目名>-<日期>-<任务>/`，不放进项目仓库。

## 六个命令

任务内容建议写进文件，再用 `-` 从标准输入传入。每个命令都输出结果摘要，记录目录里有 `result.json`；加 `--json` 只输出 JSON。

| 命令 | 作用 |
|---|---|
| `agy-ask [--role plan\|copywriting\|ask] [--isolate] [--resume ID] <目录> -` | 以只读顾问身份调用 Gemini。用单次生效的 deny 规则加沙箱禁止写入，调用前后比对目录指纹，被改动则退出码 3。`--isolate` 在临时副本里运行。 |
| `codex-worktree create\|list\|remove <仓库> <名字>` | 给 Codex 建独立工作台：`<仓库名>.worktrees/<名字>`，分支 `codex/<名字>`。删除时若有未提交改动或被忽略的本地文件（依赖、缓存除外）会拒绝，确认不要再加 `--force`。 |
| `codex-worker [--resume ID] [--allow-dirty] <工作台> -` | 让 Codex 在工作台里干活：只能写该工作台、不能联网。交回改动文件、**实际执行过的命令及退出码**、diff，以及标注"未经核实"的 Codex 汇报。 |
| `codex-review [--uncommitted\|--base 分支\|--commit SHA\|--all] [--round N --previous 上轮result.json] [--deep] <仓库>` | 用 Codex 做只读、结构化的代码审查。退出码：0 可上线；5 有挡上线问题；4 超过轮数交人决定；1 失败；124 超时。改动路径涉及钱、权限、对外消息时自动用深审档位。 |
| `review-gate install\|uninstall\|status\|reset <仓库>` | 给仓库装推送前关卡（git pre-push 钩子）。 |
| `guard-check [<仓库>] [--update-baseline]` | 执行项目里的守卫规则。 |

## 推送前关卡与守卫

`review-gate install <仓库>` 后，每次 `git push` 依次检查，前一道不过就不跑后面：

1. **项目自检**：`.multi-model-team/project.toml` 里 `[checks] commands` 列出的命令（类型检查、单元测试等）。
2. **守卫检查**：`guard-check`。
3. **Codex 审查**：审这次要推送的提交范围。

- 只有挡上线问题才拦推送，建议项只打印。
- 同一份代码已审查通过、且审查范围覆盖本次推送时，不重复审。
- 有未提交改动时拒绝推送，保证检查的就是要推的代码。
- 第二轮仍被挡、或审查本身跑不起来时，交给人决定：`MULTI_MODEL_TEAM_ALLOW_PUSH="原因" git push`，原因会记进日志。
- 状态（轮数、已审标记、放行日志）放在 `.git/multi-model-team/`，不进仓库。

**守卫规则**把"被纠正过一次的错误"变成机械检查，写在 `project.toml`：

```toml
[checks]
commands = ["python3 -m unittest discover tests"]
timeout = "10m"

[[guards]]
name = "money-no-float"
pattern = '\bparseFloat\('
files = ["src/**/*.ts"]
exclude = ["src/legacy/**"]
message = "金额一律用整数分，改用 toCents()"
```

已有违规可以记成欠账（`.multi-model-team/guard-baseline.json`），欠账只许减少；新增违规一律报错。

## 安全说明

- **Gemini 只读**：deny 规则禁止写文件和执行命令，并开沙箱；调用前后比对目录指纹。需要更保险时加 `--isolate`，原目录完全不暴露。
- **Codex 受限**：`workspace-write` 沙箱，只能写工作台，不能联网；默认要求工作台干净，便于区分 Codex 的改动。
- **不信汇报**：所有模型的汇报都只作参考，以实际执行的命令、退出码、diff 和 Claude 自己跑的测试为准。
- **本地钩子可被绕过**：`git push --no-verify` 能跳过 pre-push 钩子；需要强制保证时请配合 CI 和分支保护。
- 调用各家 CLI 会消耗你自己的订阅额度。`agy-ask` 会关闭自动扣除 AI Credits（`useG1Credits: false`）。

## 开发与测试

```bash
scripts/check.sh                            # 一次跑完下面全部检查（与 CI 一致）
python3 -m unittest discover tests          # 全部用例（假 CLI，不调用真模型）
ruff check . && ruff format --check .       # 代码规范
coverage run -m unittest discover tests && coverage combine && coverage report   # 覆盖率，低于 80% 失败
shellcheck scripts/*.sh
```

CI（GitHub Actions）在 Ubuntu 和 macOS × Python 3.11–3.14 上跑全部测试，另跑 ruff、覆盖率门槛和 shellcheck。设计说明见 [docs/design.md](docs/design.md)。

## 致谢

分工思路受数字生命卡兹克的公众号文章《我是如何让 Claude、GPT 和 Gemini 完美协同的》启发。

## 许可证

[MIT](LICENSE)
