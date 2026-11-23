import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from personalbench.runners import RunnerError, execute, run_candidate, token_cost


def is_running(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return (
        result.returncode == 0
        and bool(result.stdout.strip())
        and not result.stdout.lstrip().startswith("Z")
    )


class RunnerTests(unittest.TestCase):
    def task(self):
        return {"id": "a", "brief": "Synthetic question", "inputs": {}}

    def config(self, script, *args, **extra):
        return {
            "name": "Synthetic process",
            "harness": "command",
            "model": "synthetic",
            "command": [sys.executable, "-c", script, *args],
            **extra,
        }

    def test_native_execution_requires_explicit_authorization(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RunnerError, "allow-unsafe-local"),
        ):
            run_candidate(self.task(), self.config("print('hello')"), Path(directory))

    def test_environment_is_minimal_and_allowlist_is_explicit(self):
        script = (
            "import os,json;print(json.dumps({'output':json.dumps(dict(os.environ)),'usage':{}}))"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"PERSONALBENCH_TEST_SECRET": "synthetic-value"},  # pragma: allowlist secret
            ),
        ):
            result = run_candidate(
                self.task(), self.config(script), Path(directory), allow_unsafe_local=True
            )
            self.assertEqual(result["status"], "completed")
            environment = json.loads(result["output"])
            self.assertNotIn("PERSONALBENCH_TEST_SECRET", environment)
            self.assertNotEqual(environment["HOME"], str(Path.home()))
            config = self.config(script, env_allowlist=["PERSONALBENCH_TEST_SECRET"])
            allowed = run_candidate(self.task(), config, Path(directory), allow_unsafe_local=True)
            self.assertEqual(
                json.loads(allowed["output"])["PERSONALBENCH_TEST_SECRET"], "synthetic-value"
            )

    def test_missing_auth_or_model_access_fails_without_echoing_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "codex"
            executable.write_text(
                f'#!{sys.executable}\nimport sys\nprint("SYNTHETIC_AUTH_FAILURE_DETAIL",file=sys.stderr)\nsys.exit(1)\n'
            )
            executable.chmod(0o700)
            empty_home = root / "empty-auth-home"
            empty_home.mkdir()
            with patch.dict(os.environ, {"PATH": str(root), "CODEX_HOME": str(empty_home)}):
                result = run_candidate(
                    self.task(),
                    {"name": "Fake Codex", "harness": "codex", "model": "unavailable-synthetic"},
                    root,
                    allow_unsafe_local=True,
                )
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertNotIn("SYNTHETIC_AUTH_FAILURE_DETAIL", result["error"])
            self.assertEqual(result["output"], "")
            self.assertIsNone(result["cost_usd"])

    def test_invalid_json_nonfinite_cost_and_output_limit_are_errors(self):
        scripts = [
            "print('not json')",
            'print(\'{"output":"done","usage":{},"cost_usd":NaN}\')',
            "print('x'*100000)",
        ]
        with tempfile.TemporaryDirectory() as directory:
            for script in scripts:
                with self.subTest(script=script):
                    result = run_candidate(
                        self.task(),
                        self.config(script, max_output_bytes=1024),
                        Path(directory),
                        allow_unsafe_local=True,
                    )
                    self.assertEqual(result["status"], "infrastructure_error")
                    self.assertEqual(result["output"], "")

    def test_timeout_kills_parent_and_child_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pidfile = root / "child.pid"
            script = (
                "import subprocess,sys,time;from pathlib import Path;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                "Path(sys.argv[1]).write_text(str(child.pid));time.sleep(30)"
            )
            result = run_candidate(
                self.task(),
                self.config(script, str(pidfile), timeout_seconds=0.2),
                root,
                allow_unsafe_local=True,
            )
            self.assertEqual(result["status"], "budget_exceeded")
            pid = int(pidfile.read_text())
            try:
                deadline = time.monotonic() + 2
                while is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(is_running(pid), "Timed-out candidate child remained alive")
            finally:
                if is_running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_completed_parent_does_not_leave_background_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = "import subprocess,sys;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(child.pid,flush=True)"
            code, output, _ = execute(
                [sys.executable, "-c", script], "", root, {"PATH": os.defpath}, 2
            )
            self.assertEqual(code, 0)
            pid = int(output.strip())
            try:
                deadline = time.monotonic() + 1
                while is_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(
                    is_running(pid), "Completed candidate left an untracked background process"
                )
            finally:
                if is_running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_cost_accounts_for_cached_tokens_and_rejects_bad_usage(self):
        config = {
            "prices": {
                "input_per_million": 2,
                "cached_input_per_million": 1,
                "output_per_million": 4,
            }
        }
        cost, kind = token_cost(
            {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 100}, config
        )
        self.assertAlmostEqual(cost, 0.0019)
        self.assertEqual(kind, "estimated")
        self.assertIsNone(
            token_cost({"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 1}, config)[0]
        )
        self.assertIsNone(token_cost({}, config)[0])


def test_codex_event_parser_preserves_unicode_line_separators():
    from personalbench.runners import _parse_codex

    text = "First\u2028second\u2029third\u0085fourth"
    stdout = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    )
    output, usage = _parse_codex(stdout)
    assert output == text
    assert usage["input_tokens"] == 1


def test_nonstandard_billed_token_categories_leave_estimated_cost_unknown():
    config = {
        "prices": {"input_per_million": 1, "cached_input_per_million": 0.1, "output_per_million": 5}
    }
    for extra in ("cache_write_input_tokens", "cache_creation_input_tokens", "image_tokens"):
        usage = {"input_tokens": 100, "output_tokens": 10, extra: 20}
        assert token_cost(usage, config) == (None, "unknown")
