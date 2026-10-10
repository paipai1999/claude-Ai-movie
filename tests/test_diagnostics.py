"""
Tests for the suppressed-exception diagnostics helper and for the guarantee that
no best-effort handler in the code base silently swallows errors any more.
"""
import ast
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from brain.diagnostics import log_suppressed, debug_enabled

CLEANUP_CALLS = {"remove", "unlink", "rmtree", "rmdir", "close", "kill", "terminate", "utime", "removedirs", "flush", "set"}
SCAN_DIRS = ["agents", "agents/merger", "brain", "core", "services"]
SCAN_FILES = ["main.py", "web_ui.py", "hardsub_engine.py", "subtitle_engine.py", "regenerate_thumbnail.py"]
# stream wrappers must stay silent: printing from inside write()/flush() would recurse
STREAM_METHODS = {"write", "flush", "isatty", "fileno", "reconfigure"}


class TestLogSuppressed(unittest.TestCase):

    def test_silent_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAI_DEBUG", None)
            buf = io.StringIO()
            with redirect_stdout(buf):
                log_suppressed("mod.func", ValueError("boom"))
            self.assertEqual(buf.getvalue(), "")
            self.assertFalse(debug_enabled())

    def test_reports_location_and_error_in_debug_mode(self):
        with patch.dict(os.environ, {"PAI_DEBUG": "1"}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                log_suppressed("agents.master.save_state", OSError("disk full"))
            text = buf.getvalue()
            self.assertIn("agents.master.save_state", text)
            self.assertIn("OSError", text)
            self.assertIn("disk full", text)

    def test_never_raises_even_when_stdout_is_broken(self):
        class Broken:
            def write(self, *_):
                raise RuntimeError("closed")

            def flush(self):
                raise RuntimeError("closed")

        with patch.dict(os.environ, {"PAI_DEBUG": "1"}), patch.object(sys, "stdout", Broken()):
            log_suppressed("x", Exception("y"))   # must not raise


class TestNoSilentHandlers(unittest.TestCase):

    def _python_files(self):
        for folder in SCAN_DIRS:
            base = os.path.join(PROJECT_ROOT, folder)
            for name in sorted(os.listdir(base)):
                # diagnostics.py is the one module that must swallow errors itself
                if name.endswith(".py") and name != "diagnostics.py":
                    yield os.path.join(base, name)
        for name in SCAN_FILES:
            yield os.path.join(PROJECT_ROOT, name)

    @staticmethod
    def _is_cleanup_only(try_node):
        body = try_node.body
        if len(body) != 1 or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
            return False
        func = body[0].value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in CLEANUP_CALLS

    def test_only_cleanup_calls_may_swallow_exceptions_silently(self):
        offenders = []
        for path in self._python_files():
            tree = ast.parse(open(path, encoding="utf-8").read())
            enclosing = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for inner in ast.walk(node):
                        enclosing[id(inner)] = node.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    silent = len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
                    if silent and not self._is_cleanup_only(node) and enclosing.get(id(handler)) not in STREAM_METHODS:
                        offenders.append(f"{os.path.relpath(path, PROJECT_ROOT)}:{handler.lineno}")
        self.assertEqual(offenders, [], "silent `except: pass` handlers found: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
