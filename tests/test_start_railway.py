import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start_railway


class StartRailwayTests(unittest.TestCase):
    def test_resolve_service_port_uses_local_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT", None)
            self.assertEqual(start_railway._resolve_service_port(), "8080")

    def test_resolve_service_port_prefers_environment(self):
        with mock.patch.dict(os.environ, {"PORT": "9090"}, clear=False):
            self.assertEqual(start_railway._resolve_service_port(), "9090")

    def test_maybe_run_in_local_venv_reexecs_when_project_venv_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("", encoding="utf-8")

            with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(start_railway.sys, "executable", str(repo_root / "global-python.exe")), \
                mock.patch.object(start_railway.sys, "argv", ["start_railway.py"]), \
                mock.patch.object(start_railway.subprocess, "call", return_value=0) as call_mock:
                result = start_railway._maybe_run_in_local_venv(repo_root)

            self.assertEqual(result, 0)
            call_mock.assert_called_once()
            args, kwargs = call_mock.call_args
            self.assertEqual(args[0][0], str(venv_python.resolve()))
            self.assertEqual(kwargs["cwd"], str(repo_root))
            self.assertEqual(kwargs["env"]["PORT"], "8080")
            self.assertEqual(kwargs["env"][start_railway.LOCAL_VENV_REEXEC_ENV], "1")


if __name__ == "__main__":
    unittest.main()
