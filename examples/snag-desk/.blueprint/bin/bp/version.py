"""version: the single source of the tool version (contract 1.6). The launcher carries the same TOOL_VERSION line.

SCHEMA_VERSION is the blueprint.json version this tool writes; it reads every version in SUPPORTED_SCHEMAS (version 1
is upgraded in memory, contract 3.3). STATE_VERSION is the state.json version it writes (4.1). MIN_SCHEMA2_TOOL is the
first tool version that reads schema 2: migrate and doctor compare a project's copy with it (3.4, 1.7).
"""
TOOL_VERSION = "1.9.2"
SCHEMA_VERSION = 2
SUPPORTED_SCHEMAS = (1, 2)
STATE_VERSION = 2
MIN_SCHEMA2_TOOL = "1.9.1"
