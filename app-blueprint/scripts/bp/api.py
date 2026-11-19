"""api: the frozen 1.x public names (contract 1.5, 1.6). The launcher star-imports this module, so every
top-level name of blueprint.py 1.3.0 stays an attribute of scripts/blueprint.py. Do not add 2.0 names here:
2.0 code is imported from its bp.<module> directly.
"""
# the module bindings of the 1.x file (test_plan_fuzz patches bp.subprocess.run)
import argparse
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path

from bp.version import (  # noqa: F401
    TOOL_VERSION, SCHEMA_VERSION,
)
from bp.core import (  # noqa: F401
    TOOL_HOME, find_root, bp_dir, now_iso, read_json, write_json, write_text_if_changed, append_log, norm,
    file_sha, die, _git, read_jsonl, read_text_safe, git_head, find_asset,
)
from bp.globs import (  # noqa: F401
    _RX_CACHE, glob_rx, glob_match, pattern_instance_rx,
)
from bp.learn import (  # noqa: F401
    ISSUES_URL, NO_CAPTURE_ENV, FEEDBACK_KINDS, store_dir, capture_on, project_identity, project_ident,
    _RX_WINPATH, _RX_UNIXPATH, _RX_EMAIL, _RX_URL, _RX_TOKEN, redact, store_append, record_incident,
    _version_tuple, installed_skill_versions,
)
from bp.plan import (  # noqa: F401
    ACTIONS, ROSTER_KINDS, STATES, GLYPH, LABEL, DEFAULT_IGNORE, WRITE_ACCESS, json_kind, LIST_SECTIONS,
    ID_SECTIONS, STRING_FIELDS, STRING_LIST_FIELDS, REQUIRED_KEYS, check_entry, check_plan_shapes, normalize_plan,
    load_plan, load_state, save_state, owner_of, phase_index, module_index, item_verify, planned_file, SKELETON,
)
from bp.data import (  # noqa: F401
    STORE_KINDS, RESOURCE_KINDS, ACCESS, SCAN_EXT, _SQL_CREATE, _SQL_DROP, _SQL_RENAME, _PRISMA_MODEL, _PRISMA_MAP,
    _ENV_KEY, _ENV_USE, bare_name, resource_names, resource_owner, validate_data, parse_sql_tables,
    parse_prisma_tables, schema_sources, code_refs, scan_data, data_view,
)
from bp.status import (  # noqa: F401
    folder_rule_segments, repo_files, effective_status, compute, summarize, next_ready, require_owner,
    set_file_state, guard_decision,
)
from bp.validate import (  # noqa: F401
    validate_plan, VALIDATION_CODES, classify_error, record_friction,
)
from bp.views.structure import (  # noqa: F401
    build_tree, tree_lines, md_cell, render_structure,
)
from bp.views.roster import (  # noqa: F401
    render_roster,
)
from bp.views.render import (  # noqa: F401
    find_template, render_map, _html_escape, render_all,
)
from bp.export import (  # noqa: F401
    shq, agenticteam_tasks,
)
from bp.install import (  # noqa: F401
    copy_tool,
)
from bp.commands.plan import (  # noqa: F401
    cmd_validate, cmd_render, CONTRACT_TEMPLATE, cmd_contracts, cmd_export, cmd_init,
)
from bp.commands.progress import (  # noqa: F401
    cmd_sync, cmd_status, cmd_owner, cmd_claim, cmd_mark, cmd_verify, cmd_guard, cmd_hook,
)
from bp.commands.data import (  # noqa: F401
    cmd_db_check,
)
from bp.commands.learn import (  # noqa: F401
    cmd_feedback, cmd_report, cmd_doctor,
)
from bp.cli import (  # noqa: F401
    main,
)

HERE = TOOL_HOME

__all__ = [
    "argparse", "csv", "_dt", "hashlib", "io", "json", "os", "platform", "re", "secrets", "shutil", "sqlite3",
    "subprocess", "sys", "traceback", "Path", "TOOL_VERSION", "SCHEMA_VERSION", "ACTIONS", "ROSTER_KINDS",
    "STATES", "GLYPH", "LABEL", "DEFAULT_IGNORE", "WRITE_ACCESS", "HERE", "find_root", "bp_dir", "now_iso",
    "read_json", "write_json", "write_text_if_changed", "append_log", "norm", "file_sha", "die", "_RX_CACHE",
    "glob_rx", "glob_match", "pattern_instance_rx", "json_kind", "LIST_SECTIONS", "ID_SECTIONS", "STRING_FIELDS",
    "STRING_LIST_FIELDS", "REQUIRED_KEYS", "check_entry", "check_plan_shapes", "normalize_plan", "load_plan",
    "load_state", "save_state", "owner_of", "phase_index", "module_index", "item_verify", "validate_plan",
    "cmd_validate", "ISSUES_URL", "NO_CAPTURE_ENV", "FEEDBACK_KINDS", "VALIDATION_CODES", "store_dir",
    "capture_on", "_git", "project_identity", "project_ident", "_RX_WINPATH", "_RX_UNIXPATH", "_RX_EMAIL",
    "_RX_URL", "_RX_TOKEN", "redact", "store_append", "read_jsonl", "classify_error", "record_friction",
    "record_incident", "cmd_feedback", "cmd_report", "_version_tuple", "installed_skill_versions", "cmd_doctor",
    "STORE_KINDS", "RESOURCE_KINDS", "ACCESS", "SCAN_EXT", "_SQL_CREATE", "_SQL_DROP", "_SQL_RENAME",
    "_PRISMA_MODEL", "_PRISMA_MAP", "_ENV_KEY", "_ENV_USE", "bare_name", "resource_names", "resource_owner",
    "validate_data", "parse_sql_tables", "parse_prisma_tables", "schema_sources", "read_text_safe", "code_refs",
    "scan_data", "data_view", "cmd_db_check", "folder_rule_segments", "repo_files", "git_head", "effective_status",
    "compute", "summarize", "cmd_sync", "cmd_status", "next_ready", "planned_file", "cmd_owner", "require_owner",
    "set_file_state", "cmd_claim", "cmd_mark", "cmd_verify", "guard_decision", "cmd_guard", "cmd_hook",
    "build_tree", "tree_lines", "md_cell", "render_structure", "render_roster", "find_asset", "find_template",
    "render_map", "_html_escape", "render_all", "cmd_render", "CONTRACT_TEMPLATE", "cmd_contracts", "shq",
    "agenticteam_tasks", "cmd_export", "SKELETON", "copy_tool", "cmd_init", "main",
]
