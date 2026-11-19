"""registry: the module lists and hook collection that let workstreams add behaviour without editing shared files
(contract 1.4). Imports nothing of bp statically; every module is reached through importlib.
"""
import importlib.util

COMMAND_TABLE = {   # every subcommand of 2.0 -> its module under bp
    "init": "commands.plan", "validate": "commands.plan", "render": "commands.plan", "contracts": "commands.plan",
    "export": "commands.plan", "sync": "commands.progress", "status": "commands.progress", "owner": "commands.progress",
    "claim": "commands.progress", "mark": "commands.progress", "verify": "commands.progress", "guard": "commands.progress",
    "hook": "commands.progress", "db-check": "commands.data", "feedback": "commands.learn", "report": "commands.learn",
    "doctor": "commands.learn", "migrate": "commands.migrate", "task": "commands.roadmap", "accept": "commands.records",
    "evidence": "commands.evidence", "study": "commands.study"}


def exists(name):
    """True when the module bp.<name> is present in this build (it is not imported)."""
    try:
        return importlib.util.find_spec("bp." + name) is not None
    except ModuleNotFoundError:  # a missing parent package
        return False


def command_built(command):
    """True when the module that implements this subcommand is present (contract 1.4: else it answers not-built)."""
    return command in COMMAND_TABLE and exists(COMMAND_TABLE[command])
