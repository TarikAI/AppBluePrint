"""Shared test helpers (contract 1.6): paths, the launcher and the frozen 1.3.0 tool as modules, and running the CLI.

Importing this module puts scripts/ first on sys.path, so 2.0 tests can `import bp.<module>` directly, and turns
off bytecode writing so no __pycache__ appears inside scripts/bp (the launcher does the same).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
LAUNCHER = SCRIPTS / "blueprint.py"
PACKAGE = SCRIPTS / "bp"
EXAMPLE = SKILL / "references" / "example-blueprint.json"
DATA_V1 = Path(__file__).resolve().parent / "data" / "v1"
FROZEN = DATA_V1 / "blueprint-1.3.0.py"
PY = sys.executable

sys.dont_write_bytecode = True
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TEST_STORE = tempfile.mkdtemp(prefix="bp-test-store-")  # never write to the real ~/.app-blueprint


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_launcher(name="blueprint_launcher"):
    """scripts/blueprint.py loaded by path, the way the 1.x tests load it."""
    return load_module(LAUNCHER, name)


def load_frozen(name="blueprint_frozen_1_3_0"):
    """The frozen 1.3.0 single-file tool, as a module."""
    return load_module(FROZEN, name)


def tool_env(**extra):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", APP_BLUEPRINT_HOME=TEST_STORE, APP_BLUEPRINT_NO_CAPTURE="1")
    env.pop("BLUEPRINT_OWNER", None)
    env.pop("BLUEPRINT_GUARD", None)
    env.update(extra)
    return env


def run_script(script, args, cwd, stdin=None, env=None):
    """Run a tool file (the launcher, a project copy or the frozen tool) as a subprocess."""
    return subprocess.run([PY, str(script)] + [str(a) for a in args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", input=stdin, env=env or tool_env())
