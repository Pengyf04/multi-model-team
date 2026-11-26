"""guard-check：执行项目守卫规则，已有违规记成欠账，只许减少不许增加。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import common as c
from . import project


@dataclass
class Guard:
    name: str
    regex: re.Pattern
    files: list[re.Pattern]
    exclude: list[re.Pattern]
    message: str

    def applies(self, rel: str) -> bool:
        return any(p.match(rel) for p in self.files) and not any(p.match(rel) for p in self.exclude)


@dataclass
class Report:
    counts: dict = field(default_factory=dict)  # 规则 -> 文件 -> 匹配次数
    line_counts: dict = field(default_factory=dict)  # 规则 -> 文件 -> 命中行数（旧版欠账的口径）
    hits: dict = field(default_factory=dict)  # (规则, 文件) -> [(行号, 内容)]
    new_violations: list = field(default_factory=list)  # 超出欠账的
    reducible: list = field(default_factory=list)  # 欠账可以下调的
    unregistered: list = field(default_factory=list)  # 新规则还没登记欠账


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guard-check",
        description="执行 .multi-model-team/project.toml 里的守卫规则。退出码：0 通过；1 有新违规；2 配置错误。",
    )
    p.add_argument("repo", nargs="?", default=".", help="项目目录（默认当前目录）")
    p.add_argument("--update-baseline", action="store_true", help="登记新规则的欠账、下调已减少的欠账（不允许调高）")
    p.add_argument("--json", action="store_true")
    return p


def load_guards(cfg: dict) -> list[Guard]:
    guards, names = [], set()
    for i, g in enumerate(cfg.get("guards", []), 1):
        name = g.get("name")
        if not name or name in names:
            raise project.ProjectConfigError(f"第 {i} 条守卫缺少 name 或重名")
        names.add(name)
        for key in ("pattern", "files", "message"):
            if not g.get(key):
                raise project.ProjectConfigError(f"守卫 {name} 缺少 {key}")
        try:
            regex = re.compile(g["pattern"])
        except re.error as e:
            raise project.ProjectConfigError(f"守卫 {name} 的 pattern 不是合法正则：{e}") from e
        guards.append(
            Guard(
                name,
                regex,
                [project.glob_to_regex(x) for x in g["files"]],
                [project.glob_to_regex(x) for x in g.get("exclude", [])],
                g["message"],
            )
        )
    return guards


def list_files(repo: Path) -> list[str]:
    if c.is_git_repo(repo):
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=repo, capture_output=True, check=True
        ).stdout.decode()
        return sorted(x for x in out.split("\0") if x and (repo / x).is_file())
    return sorted(
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.is_file() and not set(p.relative_to(repo).parts) & c._SKIP_DIRS
    )


COUNT_UNIT = "matches"  # 欠账文件的计数口径；旧版文件没有这个字段，按命中行数计


def scan(repo: Path, guards: list[Guard], baseline: dict, legacy: bool = False) -> Report:
    """legacy：欠账文件是旧版按行计的，比较时也按行计，免得升级后没改代码却被判新增。"""
    rep = Report()
    files = list_files(repo)
    for g in guards:
        rep.counts[g.name] = {}
        rep.line_counts[g.name] = {}
        for rel in files:
            if not g.applies(rel):
                continue
            try:
                text = (repo / rel).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # 欠账按实际匹配次数计：同一行再加一处违规也算新增
            hits = [
                (n, line.strip(), k)
                for n, line in enumerate(text.splitlines(), 1)
                if (k := sum(1 for _ in g.regex.finditer(line)))
            ]
            if hits:
                rep.counts[g.name][rel] = sum(h[2] for h in hits)
                rep.line_counts[g.name][rel] = len(hits)
                rep.hits[(g.name, rel)] = [(n, line) for n, line, _ in hits]
        if g.name not in baseline:
            if rep.counts[g.name]:
                rep.unregistered.append(g.name)
            continue
        allowed = baseline[g.name]
        current = rep.line_counts[g.name] if legacy else rep.counts[g.name]
        for rel in sorted(set(current) | set(allowed)):
            now, debt = current.get(rel, 0), allowed.get(rel, 0)
            if now > debt:
                rep.new_violations.append((g, rel, now, debt))
            elif now < debt:
                rep.reducible.append((g.name, rel, now, debt))
    return rep


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    try:
        guards = load_guards(project.load(repo))
        bpath = project.baseline_path(repo)
        data = json.loads(bpath.read_text(encoding="utf-8")) if bpath.exists() else {}
        baseline = data.get("guards", {})
        legacy = bool(baseline) and data.get("count") != COUNT_UNIT
    except (project.ProjectConfigError, json.JSONDecodeError) as e:
        print(f"guard-check：{e}")
        return c.EXIT_USAGE

    if not guards:
        c.emit("guard-check：项目没有配置守卫规则，跳过", {"status": "NO_GUARDS"}, args.json)
        return c.EXIT_OK

    rep = scan(repo, guards, baseline, legacy)
    # 新规则还没登记欠账：在 --update-baseline 前，现有违规一律算新违规
    for name in rep.unregistered:
        g = next(x for x in guards if x.name == name)
        for rel, n in rep.counts[name].items():
            rep.new_violations.append((g, rel, n, 0))

    lines: list[str] = []
    if args.update_baseline:
        increases = [v for v in rep.new_violations if v[0].name not in rep.unregistered]
        if increases:
            lines.append("✗ 拒绝更新欠账：以下文件的违规比欠账还多，欠账只许减少。先修掉新增的违规：")
            rep.new_violations = increases
            code = c.EXIT_FAILED
        else:
            new_baseline = {}
            for g in guards:
                counts = rep.counts[g.name]
                if g.name in baseline and not legacy:
                    counts = {f: min(n, baseline[g.name].get(f, 0)) for f, n in counts.items()}
                # 旧版欠账：上面已按行确认没有新增，这里直接换算成当前的匹配次数
                new_baseline[g.name] = {f: n for f, n in sorted(counts.items()) if n > 0}
            bpath.parent.mkdir(parents=True, exist_ok=True)
            c.write_json(bpath, {"count": COUNT_UNIT, "guards": new_baseline})
            total = sum(sum(v.values()) for v in new_baseline.values())
            lines.append(f"✓ 欠账已更新：{bpath}（共 {total} 处）")
            for name in rep.unregistered:
                lines.append(f"  - 新规则 {name} 登记欠账 {sum(rep.counts[name].values())} 处")
            for name, rel, now, debt in rep.reducible:
                lines.append(f"  - {name} 在 {rel} 的欠账 {debt} → {now}")
            rep.new_violations = []
            code = c.EXIT_OK
    else:
        code = c.EXIT_FAILED if rep.new_violations else c.EXIT_OK
        lines.append("✓ 守卫检查通过" if code == 0 else f"✗ 守卫检查发现 {len(rep.new_violations)} 处新违规")

    for g, rel, now, debt in rep.new_violations:
        lines.append(f"\n[{g.name}] {rel}：{now} 处（欠账 {debt}）")
        for n, text in rep.hits.get((g.name, rel), [])[:10]:
            lines.append(f"  {rel}:{n}  {text[:120]}")
        lines.append(f"  怎么改：{g.message}")
    if not args.update_baseline and rep.reducible:
        lines.append("\n欠账减少了，可运行 `guard-check --update-baseline` 下调：")
        lines += [f"  - {n} 在 {f}：{d} → {w}" for n, f, w, d in rep.reducible]

    result = {
        "status": "PASS" if code == 0 else "FAIL",
        "new_violations": [{"guard": g.name, "file": f, "count": n, "debt": d} for g, f, n, d in rep.new_violations],
        "reducible": [{"guard": n, "file": f, "count": w, "debt": d} for n, f, w, d in rep.reducible],
    }
    c.emit("\n".join(lines), result, args.json)
    return code
