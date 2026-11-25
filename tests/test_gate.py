"""guard-check 与 review-gate 的测试：守卫欠账规则、钩子安装、推到本地模拟远程仓库的各种情形。"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_wrappers import BLOCKING, Base, git

PASS_JSON = json.dumps({"summary": "没问题", "findings": []})


class GuardTest(Base):
    def config(self, extra=""):
        d = self.repo / ".multi-model-team"
        d.mkdir(exist_ok=True)
        (d / "project.toml").write_text(
            """
[[guards]]
name = "no-float"
pattern = 'parseFloat\\('
files = ["src/**/*.ts"]
exclude = ["src/legacy/**"]
message = "金额用整数分，改用 toCents()"
"""
            + extra
        )

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_no_guards_passes(self):
        code, r, _ = self.run_bin("guard-check", str(self.repo))
        self.assertEqual((code, r["status"]), (0, "NO_GUARDS"))

    def test_new_rule_debt_then_ratchet(self):
        self.config()
        self.write("src/a.ts", "parseFloat(x)\nparseFloat(y)\n")
        self.write("src/deep/b.ts", "parseFloat(z)\n")
        self.write("src/legacy/old.ts", "parseFloat(q)\n")  # 排除
        self.write("src/c.js", "parseFloat(q)\n")  # 不适用
        code, r, res = self.run_bin("guard-check", str(self.repo))
        self.assertEqual(code, 1, "新规则未登记欠账前，现有违规算新违规")
        self.assertEqual(sorted(v["file"] for v in r["new_violations"]), ["src/a.ts", "src/deep/b.ts"])

        code, _, _ = self.run_bin("guard-check", str(self.repo), "--update-baseline")
        self.assertEqual(code, 0)
        baseline = json.loads((self.repo / ".multi-model-team/guard-baseline.json").read_text())
        self.assertEqual(baseline["guards"]["no-float"], {"src/a.ts": 2, "src/deep/b.ts": 1})
        self.assertEqual(self.run_bin("guard-check", str(self.repo))[0], 0)

        # 新代码出现违规：拦住，并给出怎么改
        self.write("src/new.ts", "const p = parseFloat(price)\n")
        code, r, _ = self.run_bin("guard-check", str(self.repo))
        self.assertEqual(code, 1)
        self.assertEqual(r["new_violations"][0]["file"], "src/new.ts")
        out = subprocess.run(
            [str(Path(__file__).parents[1] / "bin/guard-check"), str(self.repo)],
            env=self.env,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("src/new.ts:1", out)
        self.assertIn("改用 toCents()", out)

        # 想把新违规登记进欠账：拒绝
        code, _, _ = self.run_bin("guard-check", str(self.repo), "--update-baseline")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads((self.repo / ".multi-model-team/guard-baseline.json").read_text()), baseline)

        # 修掉新违规并减少旧欠账：通过，并可下调
        (self.repo / "src/new.ts").unlink()
        self.write("src/a.ts", "toCents(x)\nparseFloat(y)\n")
        code, r, _ = self.run_bin("guard-check", str(self.repo))
        self.assertEqual(code, 0)
        self.assertEqual(r["reducible"], [{"guard": "no-float", "file": "src/a.ts", "count": 1, "debt": 2}])
        self.run_bin("guard-check", str(self.repo), "--update-baseline")
        baseline = json.loads((self.repo / ".multi-model-team/guard-baseline.json").read_text())
        self.assertEqual(baseline["guards"]["no-float"], {"src/a.ts": 1, "src/deep/b.ts": 1})

    def test_bad_regex_is_config_error(self):
        self.config("""
[[guards]]
name = "bad"
pattern = '('
files = ["*"]
message = "x"
""")
        code, _, res = self.run_bin("guard-check", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("不是合法正则", res.stdout)


class GateBase(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        code, _, _ = self.gate("install", str(self.repo))
        self.assertEqual(code, 0)

    def gate(self, *args, env=None):
        res = subprocess.run(
            [str(Path(__file__).parents[1] / "bin/review-gate"), *args],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
        )
        return res.returncode, None, res

    def push(self, review=PASS_JSON, env=None, *extra):
        res = subprocess.run(
            ["git", "push", "-q", "origin", "main", *extra],
            cwd=self.repo,
            env={**self.env, "FAKE_REVIEW_JSON": review, **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.returncode, res.stdout + res.stderr

    def commit(self, name="b.txt", text="x\n"):
        (self.repo / name).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", f"change {name}")

    def remote_head(self):
        res = subprocess.run(["git", "rev-parse", "main"], cwd=self.remote, capture_output=True, text=True)
        return res.stdout.strip() if res.returncode == 0 else None

    def review_calls(self):
        return [c for c in self.calls() if "--output-schema" in c["argv"]]


class GateInstallTest(GateBase):
    def test_install_status_uninstall(self):
        hook = self.repo / ".git/hooks/pre-push"
        self.assertIn("multi-model-team review-gate", hook.read_text())
        _, _, res = self.gate("status", str(self.repo))
        self.assertIn("已安装", res.stdout)
        self.assertEqual(self.gate("uninstall", str(self.repo))[0], 0)
        self.assertFalse(hook.exists())

    def test_refuses_foreign_hook_and_hooks_path(self):
        self.gate("uninstall", str(self.repo))
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\necho mine\n")
        code, _, res = self.gate("install", str(self.repo))
        self.assertEqual(code, 2)
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho mine\n")
        hook.unlink()
        git(self.repo, "config", "core.hooksPath", ".husky")
        code, _, res = self.gate("install", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("core.hooksPath", res.stdout)


class GatePushTest(GateBase):
    def test_first_push_reviews_whole_repo(self):
        code, out = self.push()
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.remote_head(),
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip(),
        )
        self.assertIn("整个仓库", self.review_calls()[0]["stdin"])

    def test_blocked_then_second_round_passes(self):
        self.push()
        pushed = self.remote_head()
        self.commit()
        code, out = self.push(review=BLOCKING)
        self.assertNotEqual(code, 0)
        self.assertIn("第 2 轮复审", out)
        self.assertIn("金额算错", out)
        self.assertEqual(self.remote_head(), pushed, "被挡时远程不应更新")
        base_sha = pushed
        self.assertIn(f"git diff {base_sha}...", self.review_calls()[-1]["stdin"])

        self.commit("fix.txt")
        code, out = self.push()
        self.assertEqual(code, 0, out)
        last = self.review_calls()[-1]
        self.assertIn("第二轮", last["stdin"])
        self.assertIn("金额算错", last["stdin"])
        _, _, res = self.gate("status", str(self.repo))
        self.assertNotIn("被挡中", res.stdout)

    def test_two_blocked_rounds_escalate_then_allow(self):
        self.push()
        self.commit()
        self.push(review=BLOCKING)
        self.commit("try2.txt")
        code, out = self.push(review=BLOCKING)
        self.assertNotEqual(code, 0)
        self.assertIn("需要你来决定", out)
        n = len(self.review_calls())
        code, out = self.push()
        self.assertNotEqual(code, 0)
        self.assertIn("需要你来决定", out)
        self.assertEqual(len(self.review_calls()), n, "超过轮数不应再调用 Codex")

        code, out = self.push(env={"MULTI_MODEL_TEAM_ALLOW_PUSH": "演示环境，已人工确认"})
        self.assertEqual(code, 0, out)
        log = (self.repo / ".git/multi-model-team/gate.log").read_text()
        self.assertIn("ALLOW 演示环境，已人工确认", log)

        self.assertEqual(self.gate("reset", str(self.repo))[0], 0)

    def test_dirty_worktree_refused(self):
        (self.repo / "a.txt").write_text("dirty\n")
        code, out = self.push()
        self.assertNotEqual(code, 0)
        self.assertIn("未提交的改动", out)
        self.assertEqual(self.review_calls(), [])

    def test_failing_check_command_blocks_before_review(self):
        d = self.repo / ".multi-model-team"
        d.mkdir()
        (d / "project.toml").write_text('[checks]\ncommands = ["echo 测试挂了 && exit 3"]\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "cfg")
        code, out = self.push()
        self.assertNotEqual(code, 0)
        self.assertIn("自检命令失败", out)
        self.assertIn("测试挂了", out)
        self.assertEqual(self.review_calls(), [])

    def test_guard_violation_blocks(self):
        d = self.repo / ".multi-model-team"
        d.mkdir()
        (d / "project.toml").write_text(
            '[[guards]]\nname = "no-todo"\npattern = "TODO"\nfiles = ["**"]\nmessage = "不要留 TODO"\n'
        )
        (self.repo / "x.py").write_text("# TODO later\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "todo")
        code, out = self.push()
        self.assertNotEqual(code, 0)
        self.assertIn("不要留 TODO", out)

    def test_review_failure_blocks_with_allow_hint(self):
        code, out = self.push(review="不是JSON")
        self.assertNotEqual(code, 0)
        self.assertIn("MULTI_MODEL_TEAM_ALLOW_PUSH", out)

    def test_already_reviewed_code_is_not_reviewed_again(self):
        self.push()
        (self.repo / "feature.txt").write_text("new\n")
        code, _, _ = self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": PASS_JSON})
        self.assertEqual(code, 0)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "feature")
        n = len(self.review_calls())
        code, out = self.push()
        self.assertEqual(code, 0, out)
        self.assertIn("已审查通过，不重复审", out)
        self.assertEqual(len(self.review_calls()), n)

    def test_stamp_not_trusted_when_push_covers_more(self):
        self.push()
        self.commit("unreviewed.txt")  # 这个提交没审过
        (self.repo / "feature.txt").write_text("new\n")
        self.run_bin("codex-review", str(self.repo), env={"FAKE_REVIEW_JSON": PASS_JSON})
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "feature")
        n = len(self.review_calls())
        code, out = self.push()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.review_calls()), n + 1, "推送范围比审过的大，应重新审")

    def test_tag_push_skips_checks(self):
        self.push()
        git(self.repo, "tag", "v1")
        (self.repo / "a.txt").write_text("dirty\n")  # 只推标签时不检查
        res = subprocess.run(
            ["git", "push", "-q", "origin", "v1"], cwd=self.repo, env=self.env, capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0, res.stderr)


class GateTargetTest(GateBase):
    """review-gate 第 1 轮审查挡回问题的回归测试。"""

    def head(self):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()

    def raw_push(self, *refspec, env=None):
        res = subprocess.run(
            ["git", "push", "-q", "origin", *refspec],
            cwd=self.repo,
            env={**self.env, "FAKE_REVIEW_JSON": PASS_JSON, **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.returncode, res.stdout + res.stderr

    def test_push_by_sha_is_checked(self):
        (self.repo / "a.txt").write_text("dirty\n")
        code, out = self.raw_push(f"{self.head()}:refs/heads/main")
        self.assertNotEqual(code, 0, "用提交 SHA 推送也必须经过关卡")
        self.assertIn("未提交的改动", out)
        (self.repo / "a.txt").write_text("hello\n")
        code, out = self.raw_push(f"{self.head()}:refs/heads/main")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.review_calls()), 1)

    def test_non_fast_forward_push_needs_human(self):
        self.push()
        self.commit("fix.txt")
        self.push()
        fixed = self.remote_head()
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")  # 回退到首次推送、已有全量"已审"标记的提交
        n = len(self.review_calls())
        code, out = self.push(PASS_JSON, None, "--force")
        self.assertNotEqual(code, 0, "强推回退不能复用旧的已审标记放行")
        self.assertIn("不是快进", out)
        self.assertEqual(self.remote_head(), fixed)
        self.assertEqual(len(self.review_calls()), n)
        code, out = self.push(PASS_JSON, {"MULTI_MODEL_TEAM_ALLOW_PUSH": "确认回退"}, "--force")
        self.assertEqual(code, 0, out)

    def test_round_state_keys_do_not_collide(self):
        self.push()
        self.commit("c1.txt")
        code, _ = self.raw_push("HEAD:refs/heads/release/api", env={"FAKE_REVIEW_JSON": BLOCKING})
        self.assertNotEqual(code, 0)
        code, out = self.raw_push("HEAD:refs/heads/release_api")
        self.assertEqual(code, 0, out)
        self.assertNotIn("第二轮", self.review_calls()[-1]["stdin"], "不同分支不能共用复审状态")
        _, _, res = self.gate("status", str(self.repo))
        self.assertIn("refs/heads/release/api", res.stdout)
        self.assertEqual(self.gate("reset", str(self.repo), "--branch", "release/api")[0], 0)
        _, _, res = self.gate("status", str(self.repo))
        self.assertNotIn("被挡中", res.stdout)

    def test_second_round_covers_all_changes_since_last_round(self):
        self.push()
        self.commit("c1.txt")
        first = self.head()
        self.push(review=BLOCKING)
        self.commit("c2.txt")
        code, out = self.push()
        self.assertEqual(code, 0, out)
        last = self.review_calls()[-1]["stdin"]
        self.assertIn("第二轮", last)
        self.assertIn(f"git diff {first} {self.head()}", last)

    def test_stale_tracking_ref_does_not_skip_review(self):
        self.push()
        n = len(self.review_calls())
        empty = self.tmp / "empty.git"
        subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
        git(self.repo, "remote", "set-url", "origin", str(empty))
        shutil.rmtree(self.repo / ".git/multi-model-team/review-pass")  # 排除"已审"标记的影响
        code, out = self.push()
        self.assertEqual(code, 0, out)
        self.assertNotIn("没有新提交", out)
        self.assertEqual(len(self.review_calls()), n + 1)
        self.assertIn("整个仓库", self.review_calls()[-1]["stdin"])

    def test_rounds_bound_to_target_and_base(self):
        init = self.head()
        self.raw_push(f"{init}:refs/heads/old")
        self.commit("c1.txt")
        self.push()
        self.commit("c2.txt")
        code, _ = self.push(review=BLOCKING)
        self.assertNotEqual(code, 0)
        code, out = self.raw_push("HEAD:refs/heads/old")
        self.assertEqual(code, 0, out)
        last = self.review_calls()[-1]["stdin"]
        self.assertNotIn("第二轮", last, "换了推送目标和范围，应从第 1 轮全面审查")
        self.assertIn(f"git diff {init}...", last)


if __name__ == "__main__":
    unittest.main()
