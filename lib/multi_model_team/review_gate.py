"""review-gate：推送前关卡（git pre-push 钩子）。

推送时依次执行：① 项目自检命令 ② 守卫检查 ③ Codex 审查。前一道不过就拦住推送。
确认要放行时：MULTI_MODEL_TEAM_ALLOW_PUSH="原因" git push
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import common as c
from . import gate_state, guard_check, project

MARKER = "# multi-model-team review-gate"
ALLOW_ENV = "MULTI_MODEL_TEAM_ALLOW_PUSH"
ALLOW_HINT = f'确认要推送：{ALLOW_ENV}="放行原因" git push'


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review-gate", description="推送前关卡：项目自检 → 守卫检查 → Codex 审查。")
    sub = p.add_subparsers(dest="action", required=True)
    for name, help_ in (
        ("install", "给仓库装上推送前关卡"),
        ("uninstall", "卸载推送前关卡"),
        ("status", "查看关卡状态"),
        ("reset", "清空审查轮数，重新从第 1 轮算"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("repo", nargs="?", default=".")
        if name == "reset":
            sp.add_argument("--branch", help="只清某个推送目标，如 origin:main（默认全部）")
    hk = sub.add_parser("hook", help="由 git pre-push 钩子调用，不需要手动运行")
    hk.add_argument("remote_name", nargs="?", default="origin")
    hk.add_argument("remote_url", nargs="?", default="")
    return p


# ---------- 安装 ----------


def hook_file(repo: Path) -> Path:
    return Path(c.git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks").strip()) / "pre-push"


def hook_script() -> str:
    exe = c.ROOT / "bin" / "review-gate"
    return f'#!/bin/sh\n{MARKER}（卸载：review-gate uninstall <仓库>）\nexec "{exe}" hook "$@"\n'


def install(repo: Path) -> int:
    hooks_path = c.git(repo, "config", "--get", "core.hooksPath", check=False).strip()
    line = f'"{c.ROOT / "bin" / "review-gate"}" hook "$@" || exit 1'
    if hooks_path:
        print(
            f"review-gate：这个仓库用 core.hooksPath={hooks_path} 管理钩子（比如 husky），没有自动安装。\n"
            f"请在该目录的 pre-push 钩子里加一行：\n  {line}"
        )
        return c.EXIT_USAGE
    hook = hook_file(repo)
    if hook.exists() and MARKER not in hook.read_text(encoding="utf-8", errors="ignore"):
        print(f"review-gate：已有别的 pre-push 钩子（{hook}），没有覆盖。\n可在其中加一行：\n  {line}")
        return c.EXIT_USAGE
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(hook_script(), encoding="utf-8")
    hook.chmod(0o755)
    cfg = project.load(repo)
    print(f"✓ 已安装推送前关卡：{hook}")
    if not cfg.get("checks", {}).get("commands"):
        print(
            f"  提示：还没配置项目自检命令。在 {project.config_path(repo)} 里写：\n"
            '  [checks]\n  commands = ["npm run typecheck", "npm test"]'
        )
    return c.EXIT_OK


def uninstall(repo: Path) -> int:
    hook = hook_file(repo)
    if not hook.exists():
        print("review-gate：没有安装推送前关卡")
        return c.EXIT_OK
    if MARKER not in hook.read_text(encoding="utf-8", errors="ignore"):
        print(f"review-gate：{hook} 不是本工具装的，没有删除")
        return c.EXIT_USAGE
    hook.unlink()
    print(f"✓ 已卸载推送前关卡：{hook}")
    return c.EXIT_OK


def status(repo: Path) -> int:
    hook = hook_file(repo)
    installed = hook.exists() and MARKER in hook.read_text(encoding="utf-8", errors="ignore")
    cfg = project.load(repo)
    lines = [
        f"推送前关卡：{'已安装' if installed else '未安装'}（{hook}）",
        f"项目自检命令：{cfg.get('checks', {}).get('commands') or '未配置'}",
        f"守卫规则：{len(cfg.get('guards', []))} 条",
    ]
    rounds_dir = gate_state.state_dir(repo) / "rounds"
    for p in sorted(rounds_dir.glob("*.json")) if rounds_dir.exists() else []:
        lines.append(f"被挡中的推送目标：{p.stem}，已审 {json.loads(p.read_text())['round']} 轮")
    log = gate_state.state_dir(repo) / "gate.log"
    if log.exists():
        lines.append("最近记录：")
        lines += ["  " + x for x in log.read_text(encoding="utf-8").splitlines()[-5:]]
    print("\n".join(lines))
    return c.EXIT_OK


# ---------- 推送时 ----------


def is_zero(sha: str) -> bool:
    return set(sha) == {"0"}


def object_exists(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True).returncode == 0


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


def remote_heads(repo: Path, remote: str) -> list[str] | None:
    """向远程实际查询现有分支的提交；查不到（离线、无权限）返回 None。不用本地 refs/remotes，它可能已过期。"""
    res = subprocess.run(
        ["git", "ls-remote", "--heads", remote], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if res.returncode != 0:
        return None
    return [line.split()[0] for line in res.stdout.splitlines() if line.strip()]


def push_base(repo: Path, remote: str, remote_sha: str, local_sha: str) -> str | None:
    """本次推送的审查起点；None 表示要审整个仓库。

    以 git 传给钩子的远端旧提交为准：
    - 远端已有该分支且本次是快进：从远端旧提交审起；
    - 非快进（强推回退、改写历史）或本地没有远端旧提交：基准无法确认，审整个仓库；
    - 远端新建分支：向远程查询现有分支，取与本次推送最近的公共祖先；查不到就审整个仓库。"""
    if not is_zero(remote_sha):
        if object_exists(repo, remote_sha) and is_ancestor(repo, remote_sha, local_sha):
            return remote_sha
        return None
    heads = remote_heads(repo, remote)
    if not heads:
        return None
    best, best_depth = None, -1
    for sha in heads:
        if not object_exists(repo, sha):
            continue
        mb = c.git(repo, "merge-base", sha, local_sha, check=False).strip()
        if not mb:
            continue
        depth = int(c.git(repo, "rev-list", "--count", mb).strip() or 0)
        if depth > best_depth:
            best, best_depth = mb, depth
    return best


def fail(msg: str) -> int:
    print(f"\n✗ 推送已拦住：{msg}")
    return c.EXIT_FAILED


def run_checks(repo: Path, cfg: dict) -> str | None:
    checks = cfg.get("checks", {})
    commands = checks.get("commands", [])
    if not commands:
        print("[1/3] 项目自检：未配置，跳过")
        return None
    timeout = c.parse_duration(checks.get("timeout", "10m"))
    for cmd in commands:
        print(f"[1/3] 项目自检：{cmd}")
        try:
            res = subprocess.run(
                cmd, shell=True, cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return f"自检命令超时：{cmd}"
        if res.returncode != 0:
            tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-40:])
            print(tail)
            return f"自检命令失败（退出码 {res.returncode}）：{cmd}"
    return None


def review(repo: Path, branch: str, base: str | None) -> int:
    """branch：轮次记录的键（远端 + 目标分支）。审查基准变了就从第 1 轮重新审。"""
    tree = gate_state.head_tree(repo)
    rec = gate_state.find_pass(repo, tree, base)
    if rec:
        print(f"[3/3] Codex 审查：这份代码 {rec['time']} 已审查通过，不重复审")
        gate_state.log(repo, f"PASS(已审) {branch} {tree}")
        return c.EXIT_OK

    rounds = gate_state.load_rounds(repo, branch)
    if rounds and rounds.get("base", "") != (base or ""):
        print("  审查范围和上一轮不同，从第 1 轮重新审")
        gate_state.clear_rounds(repo, branch)
        rounds = None
    round_no = rounds["round"] + 1 if rounds else 1
    cmd = [str(c.ROOT / "bin" / "codex-review"), str(repo), "--json", "--round", str(round_no)]
    cmd += ["--base", base] if base else ["--all"]
    if rounds:
        cmd += ["--previous", rounds["result"]]
    print(f"[3/3] Codex 审查中（第 {round_no} 轮，通常 1～5 分钟）……", flush=True)
    res = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        result = json.loads(res.stdout)
    except json.JSONDecodeError:
        print(res.stdout[-2000:], res.stderr[-2000:])
        gate_state.log(repo, f"FAILED {branch} 审查输出无法解析")
        return fail(f"审查没能完成。可稍后重试，或者{ALLOW_HINT}")

    st = result.get("status")
    review_md = Path(result.get("run_dir", "")) / "review.md"
    body = review_md.read_text(encoding="utf-8") if review_md.exists() else ""
    if st == "PASS":
        gate_state.clear_rounds(repo, branch)
        gate_state.log(repo, f"PASS {branch} 第{round_no}轮 {result.get('run_dir')}")
        if result.get("advisory"):
            print(body)
        print(f"✓ Codex 审查通过（建议 {result.get('advisory', 0)} 条，不挡推送）")
        return c.EXIT_OK
    if st == "BLOCKED":
        gate_state.save_rounds(
            repo,
            branch,
            {"round": round_no, "result": str(Path(result["run_dir"]) / "result.json"), "base": base or ""},
        )
        gate_state.log(repo, f"BLOCKED {branch} 第{round_no}轮 {result.get('run_dir')}")
        print(body)
        if round_no >= int(result.get("max_rounds", 2)):
            return fail(
                f"已审 {round_no} 轮仍有挡上线问题，需要你来决定：\n"
                f"  - 继续修：修好后运行 review-gate reset，再推送重新审\n  - 放行：{ALLOW_HINT}"
            )
        return fail(f"有 {result.get('blocking')} 个挡上线问题。修好并提交后再推送，将进行第 {round_no + 1} 轮复审")
    if st == "ESCALATE":
        gate_state.log(repo, f"ESCALATE {branch}")
        return fail(
            f"已审满轮数仍未通过，需要你来决定：\n"
            f"  - 继续修：修好后运行 review-gate reset，再推送重新审\n  - 放行：{ALLOW_HINT}"
        )
    gate_state.log(repo, f"{st} {branch} {result.get('error')}")
    return fail(f"审查没能完成（{st}：{result.get('error')}）。可稍后重试，或者{ALLOW_HINT}")


def hook(args) -> int:
    repo = Path(c.git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    updates = [ln.split() for ln in sys.stdin.read().splitlines() if ln.strip()]

    allow = os.environ.get(ALLOW_ENV)
    if allow is not None:
        reason = allow.strip() or "（未写原因）"
        gate_state.log(repo, f"ALLOW {reason} {[u[0] for u in updates]}")
        print(f"⚠️ 已按放行指令跳过推送前检查。原因：{reason}")
        return c.EXIT_OK

    # 每行是 <本地引用> <本地提交> <远端引用> <远端旧提交>。按远端目标判断：本地一侧可能是提交 SHA 或 HEAD。
    updates = [u for u in updates if len(u) == 4 and u[2].startswith("refs/heads/") and not is_zero(u[1])]
    if not updates:
        return c.EXIT_OK  # 只推标签或删分支，不检查

    head = c.git_head(repo)
    for local_ref, local_sha, remote_ref, _ in updates:
        if local_sha != head:
            target = remote_ref.removeprefix("refs/heads/")
            return fail(
                f"要推到 {target} 的提交（{local_ref}）不是当前检出的提交，检查没法对上。请先检出要推送的提交再推送"
            )
    dirty = c.git_status_entries(repo)
    if dirty:
        return fail("有未提交的改动，检查的代码会和推送的不一致。请先提交或 git stash：\n  " + "\n  ".join(dirty[:10]))

    print(f"multi-model-team 推送前关卡（{args.remote_name}）")
    try:
        cfg = project.load(repo)
    except project.ProjectConfigError as e:
        return fail(str(e))

    err = run_checks(repo, cfg)
    if err:
        gate_state.log(repo, f"CHECK_FAILED {err}")
        return fail(err)

    if cfg.get("guards"):
        print("[2/3] 守卫检查")
        code = guard_check.main([str(repo)])
        if code != c.EXIT_OK:
            gate_state.log(repo, "GUARD_FAILED")
            return fail("守卫检查没通过，按上面的提示修改后再推送")
    else:
        print("[2/3] 守卫检查：未配置，跳过")

    for _, local_sha, remote_ref, remote_sha in updates:
        remote = args.remote_url or args.remote_name
        base = push_base(repo, remote, remote_sha, local_sha)
        if base == local_sha:
            print("[3/3] Codex 审查：没有新提交，跳过")
            continue
        code = review(repo, f"{args.remote_name}:{remote_ref.removeprefix('refs/heads/')}", base)
        if code != c.EXIT_OK:
            return code
    print("✓ 推送前关卡全部通过")
    return c.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "hook":
        return hook(args)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir() or not c.is_git_repo(repo):
        print(f"review-gate：不是 git 仓库：{repo}")
        return c.EXIT_USAGE
    if args.action == "install":
        return install(repo)
    if args.action == "uninstall":
        return uninstall(repo)
    if args.action == "status":
        return status(repo)
    n = gate_state.clear_rounds(repo, args.branch)
    print(f"✓ 已清空 {n} 个分支的审查轮数，下次推送从第 1 轮开始")
    return c.EXIT_OK
