"""codex-review：用 Codex 做两级代码审查（挡上线 / 建议），最多审 max_rounds 轮。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import common as c
from . import gate_state


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-review",
        description="用 Codex 审查改动。退出码：0 可上线；5 有挡上线问题；4 超过轮数需人决定；1 审查失败。",
    )
    p.add_argument("repo", help="要审查的 git 仓库")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--uncommitted", action="store_true", help="审查未提交的改动（默认）")
    target.add_argument("--base", metavar="BRANCH", help="审查当前分支相对 BRANCH 的改动")
    target.add_argument("--commit", metavar="SHA", help="审查某个提交")
    target.add_argument("--all", action="store_true", help="审查整个仓库（首次推送、没有可比较的基准时用）")
    p.add_argument("--round", type=int, default=1, help="第几轮审查（默认 1）")
    p.add_argument("--previous", metavar="RESULT_JSON", help="上一轮的 result.json，第二轮起传入")
    p.add_argument("--deep", action="store_true", help="强制深审档位")
    p.add_argument("--model", help="覆盖配置里的模型名")
    p.add_argument("--timeout", help="超时，如 30m（默认取配置）")
    p.add_argument("--run-dir", help="本次调用记录的存放目录（默认建在 runs_root 下）")
    p.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return p


def first_parent(repo: Path, commit: str) -> str | None:
    """提交的第一父提交；根提交返回 None。合并提交按相对第一父提交的差异审查。"""
    out = c.git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return out[1] if len(out) > 1 else None


def commit_range(repo: Path, commit: str) -> tuple[str | None, str]:
    """--commit 模式的比较口径：(第一父提交或 None, 提交本身的完整 SHA)。"""
    sha = c.git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    return first_parent(repo, sha), sha


def changed_files(repo: Path, args, head: str | None = None) -> list[str]:
    """head：本轮固定下来的 HEAD 提交，审查和登记"已审"都以这同一个快照为准。"""
    head = head or "HEAD"
    if args.base:
        out = c.git(repo, "diff", "--name-only", f"{args.base}...{head}")
    elif args.commit:
        parent, sha = commit_range(repo, args.commit)
        if parent:
            out = c.git(repo, "diff", "--name-only", parent, sha)
        else:
            out = c.git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
    elif args.all:
        out = c.git(repo, "ls-tree", "-r", "--name-only", head)
    else:
        out = "\n".join(line[3:].split(" -> ")[-1] for line in c.git_status_entries(repo))
    return [x for x in out.splitlines() if x.strip()]


def needs_deep(files: list[str], keywords: list[str]) -> list[str]:
    kws = [k.lower() for k in keywords]
    return [f for f in files if any(k in f.lower() for k in kws)]


def scope_text(args, files: list[str], head: str | None = None, commit_cmp: tuple | None = None) -> str:
    """审查范围写进要求里，由 Codex 自己用 git 查看。"""
    head = head or "HEAD"
    if args.base:
        how = (
            f"提交 {head} 相对 {args.base} 的改动。用 `git diff {args.base}...{head}` 查看；"
            f"文件内容以该提交为准（`git show {head}:<路径>`）。"
        )
    elif args.commit:
        parent, sha = commit_cmp or (None, args.commit)
        if parent:
            how = f"提交 {sha} 相对其第一父提交的改动（合并提交也按此口径）。用 `git diff {parent} {sha}` 查看。"
        else:
            how = f"根提交 {sha} 引入的全部内容。用 `git show {sha}` 查看。"
    elif args.all:
        how = (
            f"整个仓库在提交 {head} 的全部代码（首次推送，没有可比较的基准）。"
            f"逐个阅读下面列出的文件，以该提交的内容为准（`git show {head}:<路径>`），不要看工作区里未提交的改动。"
        )
    else:
        how = "工作区里所有未提交的改动（已暂存、未暂存、未跟踪）。用 `git diff HEAD` 查看，未跟踪的文件直接阅读。"
    listing = "\n".join(f"- {f}" for f in files)
    return f"\n## 审查范围\n\n{how}\n\n涉及的文件：\n{listing}\n"


def reviewed_state(repo: Path, args, head: str | None = None) -> tuple[str | None, str | None]:
    """返回 (被审代码的 tree, 审查范围起点)。--commit 模式不留"已审"标记。

    --all / --base 审的是提交 head 的快照；只有工作区与该快照完全一致（没有任何未提交改动）时才登记，
    否则"已审"标记可能被套到审查时 Codex 实际没看到的代码上。"""
    if args.commit:
        return None, None
    if args.all or args.base:
        head = head or c.git_head(repo)
        if not head:
            return None, None
        tree = gate_state.commit_tree(repo, head)
        if tree != gate_state.worktree_tree(repo):
            return None, None
        if args.all:
            return tree, gate_state.EMPTY_BASE
        mb = c.git(repo, "merge-base", args.base, head, check=False).strip()
        return tree, mb or None
    return gate_state.worktree_tree(repo), c.git_head(repo) or gate_state.EMPTY_BASE


def build_prompt(previous: dict | None, scope: str) -> str:
    prompt = c.prompt_text("codex-review.md") + scope
    if previous:
        blocking = [f for f in previous.get("findings", []) if f.get("severity") == "blocking"]
        prompt += (
            "\n这是第二轮或之后的审查。上一轮的挡上线问题如下（JSON）：\n"
            + json.dumps(blocking, ensure_ascii=False, indent=2)
            + "\n本轮只检查：1) 这些问题是否已修好；2) 修复过程中是否引入了新的挡上线问题。"
            "不要再提上一轮问题的延伸情况，其余发现一律记为 advisory。\n"
        )
    return prompt


def build_command(codex: str, model: str, effort: str, schema: Path, last_path: Path) -> list[str]:
    # 不用 `codex exec review`：它不支持自定义要求与范围参数同时使用，也会忽略 --output-schema。
    return [
        codex,
        "exec",
        "--ignore-user-config",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        'approval_policy="never"',
        "--json",
        "-o",
        str(last_path),
        "--output-schema",
        str(schema),
        "-",
    ]


def parse_review(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    data = json.loads(text)
    if not isinstance(data.get("findings"), list):
        raise ValueError("缺少 findings 列表")
    for f in data["findings"]:
        if f.get("severity") == "blocking" and not str(f.get("how_it_happens", "")).strip():
            f["severity"] = "advisory"
            f["note"] = "原判为挡上线，但没写出触发路径，按规则降为建议"
    return data


def render(data: dict) -> str:
    out = [f"结论：{data.get('summary', '')}", ""]
    for sev, title in (("blocking", "挡上线"), ("advisory", "建议")):
        items = [f for f in data["findings"] if f.get("severity") == sev]
        out.append(f"## {title}（{len(items)}）")
        out.append("")
        if not items:
            out.append("- 无")
        for i, f in enumerate(items, 1):
            loc = f"{f.get('file', '')}:{f['line']}" if f.get("line") else f.get("file", "")
            out.append(f"{i}. **{f.get('title', '')}**　`{loc}`")
            if f.get("how_it_happens"):
                out.append(f"   - 怎么发生：{f['how_it_happens']}")
            if f.get("suggestion"):
                out.append(f"   - 建议：{f['suggestion']}")
            if f.get("note"):
                out.append(f"   - 备注：{f['note']}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = c.load_config()
        section = cfg.get("codex_review", {})
        repo = Path(args.repo).expanduser().resolve()
        if not repo.is_dir() or not c.is_git_repo(repo):
            raise c.UsageError(f"不是 git 仓库：{repo}")
        timeout = c.parse_duration(args.timeout or section.get("timeout", "30m"))
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8")) if args.previous else None
    except (c.UsageError, OSError, json.JSONDecodeError) as e:
        print(f"codex-review：{e}")
        return c.EXIT_USAGE

    max_rounds = int(section.get("max_rounds", 2))
    run_dir = c.make_run_dir(cfg, "codex-review", args.run_dir, repo)
    base = {
        "tool": "codex-review",
        "repo": str(repo),
        "round": args.round,
        "max_rounds": max_rounds,
        "run_dir": str(run_dir),
    }

    if args.round > max_rounds:
        result = {
            **base,
            "status": "ESCALATE",
            "error": f"已审 {max_rounds} 轮仍未通过，按规则交给人决定放行还是继续修",
        }
        c.write_json(run_dir / "result.json", result)
        c.emit(f"# codex-review 结果：ESCALATE\n\n- ⚠️ {result['error']}\n- 记录目录：{run_dir}", result, args.json)
        return c.EXIT_ESCALATE

    head = c.git_head(repo)
    try:
        commit_cmp = commit_range(repo, args.commit) if args.commit else None
        files = changed_files(repo, args, head)
    except RuntimeError as e:
        print(f"codex-review：{e}")
        return c.EXIT_USAGE
    if not files:
        result = {**base, "status": "PASS", "findings": [], "summary": "没有需要审查的改动"}
        c.write_json(run_dir / "result.json", result)
        c.emit(f"# codex-review 结果：PASS\n\n- 没有需要审查的改动\n- 记录目录：{run_dir}", result, args.json)
        return c.EXIT_OK

    tree_before, reviewed_base = reviewed_state(repo, args, head)
    deep_hits = needs_deep(files, section.get("deep_path_keywords", []))
    deep = args.deep or bool(deep_hits)
    effort = section.get("deep_effort", "xhigh") if deep else section.get("effort", "high")
    model = args.model or section["model"]
    prompt = build_prompt(previous, scope_text(args, files, head, commit_cmp))
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    last_path = run_dir / "last-message.json"
    # 复用记录目录时，旧的结论文件不能被当成本轮结果
    last_path.unlink(missing_ok=True)
    cmd = build_command(cfg["paths"]["codex"], model, effort, c.ROOT / "schemas" / "review.schema.json", last_path)
    run = c.run_logged(
        cmd,
        cwd=repo,
        timeout=timeout,
        stdout_path=run_dir / "events.jsonl",
        stderr_path=run_dir / "codex.stderr.txt",
        stdin_text=prompt,
    )

    data, error = None, None
    if run.timed_out:
        status, code, error = "TIMEOUT", c.EXIT_TIMEOUT, f"超过 {timeout} 秒未结束，已终止"
    else:
        if run.returncode != 0:
            error = f"codex 异常退出（退出码 {run.returncode}），本轮结果不采信"
        else:
            try:
                data = parse_review(last_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                error = f"无法解析审查结果：{e}；codex 退出码 {run.returncode}"
        if data is None:
            status, code = "FAILED", c.EXIT_FAILED
        elif any(f["severity"] == "blocking" for f in data["findings"]):
            status, code = "BLOCKED", c.EXIT_BLOCKING
        else:
            status, code = "PASS", c.EXIT_OK

    pass_record, stamp_note = None, None
    if status == "PASS" and tree_before and reviewed_base:
        tree_after, _ = reviewed_state(repo, args, head)
        if tree_after == tree_before and c.git_head(repo) == head:
            pass_record = str(gate_state.record_pass(repo, tree_before, reviewed_base, str(run_dir / "result.json")))
        else:
            stamp_note = "审查期间代码有变化，未留下已审标记，推送时会重新审查"

    stderr_tail = (run_dir / "codex.stderr.txt").read_text(encoding="utf-8", errors="replace")[-1500:]
    findings = data["findings"] if data else []
    result = {
        **base,
        "status": status,
        "model": model,
        "effort": effort,
        "deep": deep,
        "deep_reason_files": deep_hits,
        "files": files,
        "summary": data.get("summary") if data else None,
        "blocking": sum(f["severity"] == "blocking" for f in findings),
        "advisory": sum(f["severity"] == "advisory" for f in findings),
        "findings": findings,
        "error": error,
        "reviewed_tree": tree_before,
        "reviewed_base": reviewed_base,
        "pass_record": pass_record,
        "stderr_tail": stderr_tail if status in ("FAILED", "TIMEOUT") else "",
    }
    c.write_json(run_dir / "result.json", result)
    body = render(data) if data else ""
    (run_dir / "review.md").write_text(body, encoding="utf-8")

    lines = [
        f"# codex-review 结果：{status}（第 {args.round}/{max_rounds} 轮）",
        "",
        f"- 模型：{model}（{effort}{'，深审' if deep else ''}）　审查文件数：{len(files)}",
        f"- 记录目录：{run_dir}",
    ]
    if deep_hits and not args.deep:
        lines.append(f"- 触发深审的文件：{', '.join(deep_hits[:5])}")
    if error:
        lines.append(f"- ⚠️ {error}")
    if stamp_note:
        lines.append(f"- ⚠️ {stamp_note}")
    if body:
        lines += ["", body]
    if status in ("FAILED", "TIMEOUT") and stderr_tail.strip():
        lines += ["", "## codex 错误输出（末尾）", "", "```", stderr_tail.strip(), "```"]
    c.emit("\n".join(lines), result, args.json)
    return code
