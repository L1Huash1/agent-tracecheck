"""The base package must not depend on an optional framework being installed."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class OptionalImportTests(unittest.TestCase):
    def test_base_import_stays_independent_and_missing_framework_has_install_hint(self):
        script = '''
import importlib.abc
import sys
class BlockFramework(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langchain_core" or fullname.startswith("langchain_core."):
            raise ModuleNotFoundError("blocked optional dependency", name="langchain_core")
sys.meta_path.insert(0, BlockFramework())
import agent_tracecheck
import agent_tracecheck.cli
assert not any(name.startswith("langchain_core") for name in sys.modules)
assert agent_tracecheck.analyze_trace([])["summary"]["calls"] == 0
try:
    import agent_tracecheck.integrations.langchain
except ImportError as error:
    assert 'python -m pip install ".[langchain]"' in str(error), str(error)
    assert 'source release' in str(error), str(error)
    assert 'pip install "agent-tracecheck[langchain]"' not in str(error), str(error)
else:
    raise AssertionError("optional integration imported without its framework")
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
