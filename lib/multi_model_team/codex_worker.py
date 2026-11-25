"""codex-worker：在 git 工作树里让 Codex 按固定档位、工作区沙箱写代码。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import common as c

RESUME_NOTE = "（继续之前的任务，规则不变。完成后按同样的四段结构汇报。）\n\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-worker",
        description="在 git 工作树里让 Codex 干活（只能写该工作树、不能联网）。任务写 - 则从标准输入读取。",
    )
    p.add_argument("worktree", help="Codex 工作的 git 工作树")
    p.add_argument("task", help="任务内容，或 - 从标准输入读取")
    p.add_argument("--resume", metavar="THREAD_ID", help="续接之前的 Codex 会话，追加任务")
    p.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许在有未提交改动的工作树上开始（默认要求干净，便于区分 Codex 的改动）",
    )
    p.add_argument("--model", help="覆盖配置里的模型名")
    p.add_argument("--effort", help="覆盖配置里的推理档位")
    p.add_argument("--timeout", help="超时，如 60m（默认取配置）")
    p.add_argument("--run-dir", help="本次调用记录的存放目录（默认建在 runs_root 下）")
    p.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return p


def build_command(codex: str, model: str, effort: str, last_path: Path, resume: str | None) -> list[str]:
    cmd = [codex, "exec"]
    if resume:
        cmd.append("resume")
    cmd += [
        "--ignore-user-config",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(last_path),
    ]
    if resume:
        cmd.append(resume)
    cmd.append("-")  # 任务从标准输入传入
    return cmd


def parse_events(path: Path) -> dict:
    info = {"thread_id": None, "commands": [], "messages": [], "usage": None, "errors": []}
    if not path.exists():
        return info
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            info["thread_id"] = ev.get("thread_id")
        elif t == "item.completed":
            item = ev.get("item", {})
            if item.get("type") == "command_execution":
                info["commands"].append({"command": item.get("command"), "exit_code": item.get("exit_code")})
            elif item.get("type") == "agent_message":
                info["messages"].append(item.get("text", ""))
        elif t == "turn.completed":
            info["usage"] = ev.get("usage")
        elif t in ("turn.failed", "error"):
            err = ev.get("error") or ev.get("message") or ev
            info["errors"].append(err.get("message") if isinstance(err, dict) else str(err))
    return info


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = c.load_config()
        section = cfg.get("codex_worker", {})
        wt = Path(args.worktree).expanduser().resolve()
        if not wt.is_dir() or not c.is_git_repo(wt):
            raise c.UsageError(f"不是 git 工作树：{wt}")
        if not args.resume and not args.allow_dirty and c.git_status_entries(wt):
            raise c.UsageError("工作树有未提交的改动。请先提交或暂存，或加 --allow-dirty")
        task = c.read_task(args.task)
        timeout = c.parse_duration(args.timeout or section.get("timeout", "60m"))
    except c.UsageError as e:
        print(f"codex-worker：{e}")
        return c.EXIT_USAGE

    model = args.model or section["model"]
    effort = args.effort or section["effort"]
    try:
        run_dir = c.make_run_dir(cfg, "codex-worker", args.run_dir, wt)
    except c.UsageError as e:
        print(f"codex-worker：{e}")
        return c.EXIT_USAGE
    prompt = (RESUME_NOTE + task) if args.resume else (c.prompt_text("codex-worker.md") + task)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    head_before = c.git_head(wt)
    last_path = run_dir / "last-message.md"
    events_path = run_dir / "events.jsonl"
    cmd = build_command(cfg["paths"]["codex"], model, effort, last_path, args.resume)
    run = c.run_logged(
        cmd,
        cwd=wt,
        timeout=timeout,
        stdout_path=events_path,
        stderr_path=run_dir / "codex.stderr.txt",
        stdin_text=prompt,
    )

    ev = parse_events(events_path)
    head_after = c.git_head(wt)
    changes = c.git_status_entries(wt)
    diff = c.git(wt, "diff", head_before, check=False) if head_before else c.git(wt, "diff", check=False)
    (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
    final = (
        last_path.read_text(encoding="utf-8") if last_path.exists() else (ev["messages"][-1] if ev["messages"] else "")
    )

    warnings = []
    if head_after != head_before:
        warnings.append(f"Codex 改动了提交历史（HEAD {head_before} → {head_after}），按规则它不该提交")
    if not ev["commands"]:
        warnings.append("Codex 没有执行任何命令：它汇报里提到的测试或验证都没有真正运行")
    failed = [cmd_["command"] for cmd_ in ev["commands"] if cmd_["exit_code"] not in (0, None)]
    if failed:
        warnings.append(f"有 {len(failed)} 条命令以非零退出码结束，请核对汇报是否如实")

    if run.timed_out:
        status, code, error = "TIMEOUT", c.EXIT_TIMEOUT, f"超过 {timeout} 秒未结束，已终止"
    elif run.returncode != 0 or ev["errors"]:
        status, code = "FAILED", c.EXIT_FAILED
        error = "；".join(ev["errors"]) or f"codex 退出码 {run.returncode}"
    else:
        status, code, error = "DONE", c.EXIT_OK, None

    stderr_tail = (run_dir / "codex.stderr.txt").read_text(encoding="utf-8", errors="replace")[-1500:]
    result = {
        "tool": "codex-worker",
        "status": status,
        "model": model,
        "effort": effort,
        "worktree": str(wt),
        "thread_id": ev["thread_id"] or args.resume,
        "changed_files": changes,
        "commands": ev["commands"],
        "usage": ev["usage"],
        "warnings": warnings,
        "error": error,
        "stderr_tail": stderr_tail if status != "DONE" else "",
        "diff_file": str(run_dir / "diff.patch"),
        "run_dir": str(run_dir),
    }
    c.write_json(run_dir / "result.json", result)

    lines = [
        f"# codex-worker 结果：{status}",
        "",
        f"- 模型：{model}（{effort}）　会话 ID：{result['thread_id'] or '无'}",
        f"- 工作树：{wt}",
        f"- 完整 diff：{run_dir / 'diff.patch'}　记录目录：{run_dir}",
    ]
    if error:
        lines.append(f"- ⚠️ {error}")
    for w in warnings:
        lines.append(f"- ⚠️ {w}")
    lines += ["", "## 改动文件（git status）", ""]
    lines += [f"- `{x}`" for x in changes] or ["- （无改动）"]
    lines += ["", "## Codex 实际执行的命令", ""]
    lines += [f"- [{x['exit_code']}] `{x['command']}`" for x in ev["commands"]] or ["- （无）"]
    lines += ["", "## Codex 的汇报（未经核实，以上面的事实为准）", "", final.strip() or "（无）"]
    if status != "DONE" and stderr_tail.strip():
        lines += ["", "## codex 错误输出（末尾）", "", "```", stderr_tail.strip(), "```"]
    c.emit("\n".join(lines), result, args.json)
    return code
