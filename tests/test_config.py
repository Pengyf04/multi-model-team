"""配置加载：codex / agy 路径不存在时回退到 PATH 查找。运行：python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from multi_model_team import common  # noqa: E402


def make_exe(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class ResolveExecutableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pathdir = self.tmp / "pathbin"
        self.pathdir.mkdir()
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.pathdir)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_existing_configured_path_is_used(self):
        exe = make_exe(self.tmp / "codex")
        make_exe(self.pathdir / "codex")
        self.assertEqual(common.resolve_executable(str(exe), "codex"), str(exe))

    def test_missing_path_falls_back_to_path_lookup(self):
        in_path = make_exe(self.pathdir / "codex")
        self.assertEqual(common.resolve_executable("/no/such/dir/codex", "codex"), str(in_path))

    def test_bare_command_name_is_looked_up(self):
        in_path = make_exe(self.pathdir / "agy")
        self.assertEqual(common.resolve_executable("agy", "agy"), str(in_path))

    def test_unset_uses_key_name(self):
        in_path = make_exe(self.pathdir / "agy")
        self.assertEqual(common.resolve_executable(None, "agy"), str(in_path))
        self.assertEqual(common.resolve_executable("", "agy"), str(in_path))

    def test_not_executable_falls_back(self):
        plain = self.tmp / "codex"
        plain.write_text("not executable")
        in_path = make_exe(self.pathdir / "codex")
        self.assertEqual(common.resolve_executable(str(plain), "codex"), str(in_path))

    def test_nothing_found_returns_configured_value(self):
        self.assertEqual(common.resolve_executable("/no/such/codex", "codex"), "/no/such/codex")
        self.assertEqual(common.resolve_executable(None, "codex"), "codex")

    def test_tilde_is_expanded(self):
        home = self.tmp / "home"
        (home / "bin").mkdir(parents=True)
        exe = make_exe(home / "bin" / "agy")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(common.resolve_executable("~/bin/agy", "agy"), str(exe))


class LoadConfigTest(unittest.TestCase):
    def test_load_config_resolves_paths(self):
        tmp = Path(tempfile.mkdtemp())
        pathdir = tmp / "pathbin"
        pathdir.mkdir()
        codex = make_exe(pathdir / "codex")
        cfg = tmp / "models.toml"
        cfg.write_text('[paths]\ncodex = "/no/such/codex"\nruns_root = "~/runs"\n')
        env = {"MULTI_MODEL_TEAM_CONFIG": str(cfg), "PATH": str(pathdir), "HOME": str(tmp)}
        with mock.patch.dict(os.environ, env):
            loaded = common.load_config()
        self.assertEqual(loaded["paths"]["codex"], str(codex))
        self.assertEqual(loaded["paths"]["agy"], "agy")  # 没配置、PATH 里也没有：保留命令名
        self.assertEqual(loaded["paths"]["runs_root"], str(tmp / "runs"))


if __name__ == "__main__":
    unittest.main()
