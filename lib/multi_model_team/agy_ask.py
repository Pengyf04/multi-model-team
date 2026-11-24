"""agy-ask：以只读顾问身份调用 Gemini（Antigravity CLI `agy -p`）。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import common as c

ROLES = ("ask", "plan", "copywriting")

# 单次调用生效的设置：禁止写文件和执行命令；订阅额度用完直接报错，不扣 AI Credits。
READONLY_SETTINGS = {
    "useG1Credits": False,
    "permissions": {"deny": ["write_file(*)", "command(*)"]},
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agy-ask",
        description="以只读顾问身份调用 Gemini。任务写 - 则从标准输入读取。",
    )
    p.add_argument("dir", help="让 Gemini 阅读的项目目录")
    p.add_argument("task", help="任务内容，或 - 从标准输入读取")
    p.add_argument(
        "--role", choices=ROLES, default="ask", help="plan=独立出方案；copywriting=文案与交互顾问；ask=一般提问（默认）"
    )
    p.add_argument("--isolate", action="store_true", help="在临时副本里运行，原目录完全不暴露给 Gemini")
    p.add_argument("--resume", metavar="CONVERSATION_ID", help="续接之前的会话")
    p.add_argument("--model", help="覆盖配置里的模型名")
    p.add_argument("--timeout", help="超时，如 15m（默认取配置）")
    p.add_argument("--run-dir", help="本次调用记录的存放目录（默认建在 runs_root 下）")
    p.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return p


def build_prompt(role: str, task: str, resume: bool) -> str:
    if resume:
        return task
    return c.prompt_text("agy-ask/common.md") + "\n" + c.prompt_text(f"agy-ask/{role}.md") + "\n---\n\n" + task


def build_command(agy: str, prompt: str, model: str, timeout: int, resume: str | None) -> list[str]:
    cmd = [
        agy,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--settings",
        json.dumps(READONLY_SETTINGS),
        "--sandbox",
        "--disable-slash-commands",
        "--print-timeout",
        f"{timeout}s",
    ]
    if resume:
        cmd += ["--conversation", resume]
    return cmd


def copy_for_isolation(src: Path) -> tuple[Path, Path]:
    # resolve：macOS 的 /var 是 /private/var 的链接，统一成真实路径，回答里的路径才能准确换回原目录
    tmp_root = Path(tempfile.mkdtemp(prefix="agy-ask-")).resolve()
    dst = tmp_root / src.name
    if c.is_git_repo(src):
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=src, capture_output=True, check=True
        ).stdout.decode()
        for rel in filter(None, out.split("\0")):
            s = src / rel
            if not s.exists() and not s.is_symlink():
                continue
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d, follow_symlinks=False)
        dst.mkdir(parents=True, exist_ok=True)
    else:
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*c._SKIP_DIRS))
    return tmp_root, dst


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = c.load_config()
        section = cfg.get("agy_ask", {})
        target = Path(args.dir).expanduser().resolve()
        if not target.is_dir():
            raise c.UsageError(f"目录不存在：{target}")
        task = c.read_task(args.task)
        timeout = c.parse_duration(args.timeout or section.get("timeout", "15m"))
        grace = c.parse_duration(section.get("kill_grace", "60s"))
    except c.UsageError as e:
        print(f"agy-ask：{e}")
        return c.EXIT_USAGE

    model = args.model or section["model"]
    run_dir = c.make_run_dir(cfg, "agy-ask", args.run_dir, target)
    prompt = build_prompt(args.role, task, bool(args.resume))
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    tmp_root = None
    workdir = target
    if args.isolate:
        tmp_root, workdir = copy_for_isolation(target)

    # 检查 Gemini 实际运行的目录：隔离模式下是副本，这样总指挥同时改原目录不会被误判。
    before = c.tree_fingerprint(workdir)
    cmd = build_command(cfg["paths"]["agy"], prompt, model, timeout, args.resume)
    stdout_path, stderr_path = run_dir / "agy.stdout.json", run_dir / "agy.stderr.txt"
    try:
        run = c.run_logged(cmd, cwd=workdir, timeout=timeout + grace, stdout_path=stdout_path, stderr_path=stderr_path)
        modified = c.tree_fingerprint(workdir) != before
    finally:
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)

    data, error = {}, None
    raw = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
    if raw:
        try:
            data = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError:
            error = "agy 输出不是合法 JSON"
    response = data.get("response", "") or ""
    if tmp_root:
        response = response.replace(str(workdir), str(target))
    (run_dir / "response.md").write_text(response, encoding="utf-8")

    if run.timed_out:
        status, code = "TIMEOUT", c.EXIT_TIMEOUT
        error = error or f"超过 {timeout + grace} 秒未结束，已终止"
    elif data.get("status") != "SUCCESS":
        status, code = "FAILED", c.EXIT_FAILED
        error = data.get("error") or error or f"agy 退出码 {run.returncode}"
    else:
        status, code = "SUCCESS", c.EXIT_OK
    if modified:
        status, code = "WORKSPACE_MODIFIED", c.EXIT_WORKSPACE_MODIFIED
        if args.isolate:
            error = "Gemini 在副本里改了文件，只读规则可能失效（副本已删除，原目录不受影响），请检查 agy 版本和权限设置"
        else:
            error = (
                "调用期间目录发生了变化：可能是 Gemini，也可能是你自己同时在改。需要一边改文件一边调用时请加 --isolate"
            )

    stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    result = {
        "tool": "agy-ask",
        "status": status,
        "role": args.role,
        "model": model,
        "dir": str(target),
        "isolated": args.isolate,
        "conversation_id": data.get("conversation_id"),
        "workspace_modified": modified,
        "duration_seconds": data.get("duration_seconds"),
        "usage": data.get("usage"),
        "error": error,
        "stderr_tail": stderr_tail if status != "SUCCESS" else "",
        "response_file": str(run_dir / "response.md"),
        "run_dir": str(run_dir),
    }
    c.write_json(run_dir / "result.json", result)

    lines = [
        f"# agy-ask 结果：{status}",
        "",
        f"- 角色：{args.role}　模型：{model}　会话 ID：{result['conversation_id'] or '无'}",
        f"- 记录目录：{run_dir}",
    ]
    if error:
        lines.append(f"- ⚠️ {error}")
    lines += ["", "## Gemini 回答", "", response or "（无回答）"]
    if status != "SUCCESS" and stderr_tail.strip():
        lines += ["", "## agy 错误输出（末尾）", "", "```", stderr_tail.strip(), "```"]
    c.emit("\n".join(lines), result, args.json)
    return code
