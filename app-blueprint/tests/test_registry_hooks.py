"""W0-A2: the registry and its hooks (contract 1.4) let a later workstream add behaviour without editing any file of
W0-A2. Every test here adds throwaway modules under the names the contract gives the later workstreams (bp/unknowns.py,
bp/features.py, bp/views/roadmap_md.py, bp/commands/migrate.py, assets/tab-*.js, ...) and checks they are picked up.

In process, the modules are written to a temp folder that is appended to the package path (bp.__path__ and its
subpackages'), so `import bp.<name>` finds them exactly as it would a file in scripts/bp, and nothing under scripts/
changes. End to end, a temp copy of the skill gets the files and the real CLI runs from it.
"""
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import support as S

import bp
import bp.commands
import bp.views
from bp import plan as bplan, registry, validate

from test_validate import V2_EXAMPLE as V2_EXAMPLE_TEXT, TempRoot, quiet, v1_plan, v2_plan


@contextlib.contextmanager
def extension(files):
    """Make bp.<name> importable for each "rel/path.py": source pair, without touching scripts/bp."""
    tmp = Path(tempfile.mkdtemp(prefix="bp-ext-"))
    names, added = [], []
    try:
        for rel, src in files.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src), encoding="utf-8")
            names.append("bp." + ".".join(Path(rel).with_suffix("").parts))
        for pkg, folder in ((bp, tmp), (bp.views, tmp / "views"), (bp.commands, tmp / "commands")):
            if folder.is_dir():
                pkg.__path__.append(str(folder))
                added.append((pkg, str(folder)))
        importlib.invalidate_caches()
        yield tmp
    finally:
        for pkg, folder in added:
            pkg.__path__.remove(folder)
        for n in names:
            sys.modules.pop(n, None)
        importlib.invalidate_caches()
        shutil.rmtree(tmp, ignore_errors=True)


class RegistryTest(unittest.TestCase):
    def test_lists_are_the_contracts(self):
        self.assertEqual(registry.DOMAIN, ("rules", "data", "unknowns", "records", "features", "roadmap", "evidence"))
        self.assertEqual(registry.VIEWS, ("views.roadmap_md", "views.features_md"))
        self.assertEqual(set(registry.COMMAND_TABLE), {
            "init", "validate", "render", "contracts", "export", "sync", "status", "owner", "claim", "mark", "verify",
            "guard", "hook", "db-check", "feedback", "report", "doctor", "migrate", "task", "accept", "evidence", "study"})
        self.assertEqual(registry.COMMAND_TABLE["study"], "commands.study")

    def test_modules_in_order_skipping_missing(self):
        self.assertEqual([m.__name__ for m in registry.modules("DOMAIN")], ["bp.rules", "bp.data"])
        self.assertEqual(registry.modules("VIEWS"), [])
        with extension({"roadmap.py": "", "unknowns.py": "", "views/features_md.py": ""}):
            self.assertEqual([m.__name__ for m in registry.modules("DOMAIN")],
                             ["bp.rules", "bp.data", "bp.unknowns", "bp.roadmap"])
            self.assertEqual([m.__name__ for m in registry.modules("VIEWS")], ["bp.views.features_md"])

    def test_a_broken_module_is_a_bug_not_a_missing_one(self):
        with extension({"features.py": "import bp.not_there\n"}):
            with self.assertRaises(ModuleNotFoundError) as cm:
                registry.modules("DOMAIN")
            self.assertEqual(cm.exception.name, "bp.not_there")
        with extension({"records.py": "def broken(:\n"}):
            with self.assertRaises(SyntaxError):
                registry.modules("DOMAIN")

    def test_hook_has_a_single_owner(self):
        with extension({"features.py": "def status_card(ctx, fid):\n    return ['card ' + fid]\n",
                        "records.py": "def verify_feature(args, ctx):\n    return [], 0\n"}):
            self.assertEqual(registry.hook("status_card")({}, "F-x"), ["card F-x"])
            self.assertEqual(registry.hook("verify_feature")(None, {}), ([], 0))
            self.assertIsNone(registry.hook("no_such_hook"))
        with extension({"features.py": "def status_card(ctx, fid):\n    return []\n",
                        "roadmap.py": "def status_card(ctx, fid):\n    return []\n"}):
            with self.assertRaises(RuntimeError):
                registry.hook("status_card")

    def test_collect_in_domain_order(self):
        src = "def status_lines(ctx, args):\n    return [__name__]\n"
        with extension({"roadmap.py": src, "unknowns.py": src, "features.py": src}):
            self.assertEqual(registry.collect("status_lines", {}, None),
                             [("unknowns", ["bp.unknowns"]), ("features", ["bp.features"]), ("roadmap", ["bp.roadmap"])])
        self.assertEqual(registry.collect("status_lines", {}, None), [])

    def test_refusal_first_message_wins(self):
        with extension({"unknowns.py": "def refuse(action, root, plan, path, by):\n"
                                       "    return 'blocked: ' + path if action == 'claim' else None\n",
                        "records.py": "def refuse(action, root, plan, path, by):\n"
                                      "    return 'protected: ' + path if path.startswith('.blueprint/') else None\n"}):
            self.assertEqual(registry.refusal("claim", None, {}, "a.py", "x"), "blocked: a.py")
            self.assertEqual(registry.refusal("write", None, {}, ".blueprint/state.json", "x"), "protected: .blueprint/state.json")
            self.assertIsNone(registry.refusal("write", None, {}, "a.py", "x"))
        self.assertIsNone(registry.refusal("write", None, {}, ".blueprint/state.json", "x"))

    def test_signal_is_optional_and_never_raises(self):
        registry.signal(None, "guard", "guard-unplanned")  # no bp.signals yet: nothing happens
        calls = []
        with extension({"signals.py": "CALLS = []\ndef record(root, signal, code):\n    CALLS.append((root, signal, code))\n"}):
            registry.signal("R", "verify", "verify-fail")
            self.assertEqual(sys.modules["bp.signals"].CALLS, [("R", "verify", "verify-fail")])
        with extension({"signals.py": "def record(root, signal, code):\n    raise OSError('disk full')\n"}):
            registry.signal("R", "guard", "guard-protected")  # capture never breaks a command
        self.assertEqual(calls, [])

    def test_codes_have_one_owner_and_do_not_overlap(self):
        """Review N7: a code given by two modules, or a module's example message that another module's regex claims
        first, is refused at load with both modules named."""
        with extension({"unknowns.py": "CODES = [(r'^unknown \\S+: x', 'dup-code')]\n",
                        "roadmap.py": "CODES = [(r'^task \\S+: y', 'dup-code')]\n"}):
            with self.assertRaisesRegex(RuntimeError, "dup-code.*unknowns.*roadmap"):
                registry.codes()
        with extension({"features.py": "CODES = [(r'^milestone ', 'feature-x')]\nCODE_EXAMPLES = {'feature-x': 'milestone of F'}\n",
                        "roadmap.py": "CODES = [(r'^milestone \\S+: needs', 'milestone-incomplete')]\n"
                                      "CODE_EXAMPLES = {'milestone-incomplete': 'milestone M1: needs name and goal'}\n"}):
            with self.assertRaisesRegex(RuntimeError, "milestone M1: needs name and goal.*features.*roadmap"):
                registry.codes()
        with extension({"unknowns.py": "CODES = [(r'^x', 'duplicate-id')]\n"}):
            with self.assertRaisesRegex(RuntimeError, "duplicate-id.*unknowns.*rules"):
                registry.codes()
        with extension({"roadmap.py": "CODES = [(r'^task \\S+: needs title', 'task-incomplete')]\n"
                                      "CODE_EXAMPLES = {'task-incomplete': 'task T-x: needs title and owner'}\n"}):
            self.assertEqual(registry.codes()[0], (r"^task \S+: needs title", "task-incomplete"))

    def test_codes_order(self):
        with extension({"roadmap.py": "CODES = [(r'^task \\S+: needs title', 'task-incomplete')]\n",
                        "unknowns.py": "CODES = [(r'^unknown \\S+: needs question', 'unknown-incomplete')]\n"}):
            got = registry.codes()
            from bp import data, rules
            self.assertEqual(got, [(r"^unknown \S+: needs question", "unknown-incomplete"),
                                   (r"^task \S+: needs title", "task-incomplete")] + data.CODES + rules.CODES)
            self.assertEqual(validate.codes()[1:], got)


class ValidationRuleExtensionTest(TempRoot):
    """A later workstream adds a validation rule with a new code (contract 1.4, 7.4) by adding its module."""

    UNKNOWNS = '''
        CODES = [(r"^unknown \\S+: needs question, severity and status", "unknown-incomplete"),
                 (r"^unknown \\S+: severity must be", "unknown-severity")]
        SEEN = []

        def validate(plan, E, W, vctx):
            SEEN.append(dict(vctx))
            for u in plan.get("unknowns", []):
                if u.get("severity") not in ("low", "medium", "high", "critical"):
                    E(f"unknown {u['id']}: severity must be low, medium, high or critical", code="unknown-severity",
                      entry=f"unknown {u['id']}", next="set severity in blueprint.json")
                if not u.get("question"):
                    E(f"unknown {u['id']}: needs question, severity and status")  # no code: classified by CODES
                if u.get("status") == "open" and u.get("severity") == "high":
                    W(f"unknown {u['id']} (high) is open and blocks {', '.join(u.get('blocks', []))}")
    '''

    def test_rule_findings_codes_and_order(self):
        plan = v1_plan()
        plan["unknowns"] = [{"id": "U-a", "question": "", "severity": "huge", "status": "open"},
                            {"id": "U-b", "question": "q", "severity": "high", "status": "open", "blocks": ["P2"]}]
        self.write_plan(plan)
        loaded = quiet(bplan.load_plan, self.root)[0]
        with extension({"unknowns.py": self.UNKNOWNS}):
            found = validate.validate_findings(loaded, team=None, evidence_ids={"ev-1"}, root=self.root)
            mod = sys.modules["bp.unknowns"]
            self.assertEqual(mod.SEEN, [{"team": None, "evidenceIds": {"ev-1"}, "root": self.root}])
            self.assertEqual(validate.classify_error("unknown U-z: severity must be low"), "unknown-severity")
        self.assertEqual(validate.classify_error("unknown U-z: severity must be low"), "other")
        got = [(f["level"], f["code"], f["entry"], f["next"]) for f in found]
        self.assertEqual(got[0][:2], ("warning", "schema-v1"))
        self.assertEqual(got[1][:2], ("warning", "other"))  # the 1.x data warning comes before the new rules
        self.assertEqual(got[2:], [("error", "unknown-severity", "unknown U-a", "set severity in blueprint.json"),
                                   ("error", "unknown-incomplete", None, None),
                                   ("warning", "other", None, None)])
        errors, warnings = None, None
        with extension({"unknowns.py": self.UNKNOWNS}):
            errors, warnings = validate.validate_plan(loaded)
        self.assertEqual(errors, ["unknown U-a: severity must be low, medium, high or critical",
                                  "unknown U-a: needs question, severity and status"])

    def test_validation_context_comes_from_hooks(self):
        """Review M5: validate_context(root, plan) -> dict, merged into vctx; evidence supplies evidenceIds."""
        exts = {"unknowns.py": self.UNKNOWNS,
                "evidence.py": "def validate_context(root, plan):\n    return {'evidenceIds': {'ev-1'}, 'studyCount': 0}\n"}
        self.write_plan(dict(v2_plan()))
        with extension(exts):
            code, _, err = run_cli("validate", "--root", self.root)
            self.assertEqual(code, 0, err)
            seen = sys.modules["bp.unknowns"].SEEN
            got = dict(seen[-1])
            self.assertTrue(os.path.samefile(got.pop("root"), self.root))  # CI temp folders come in 8.3 short form
            self.assertEqual(got, {"team": None, "evidenceIds": {"ev-1"}, "studyCount": 0})
            run_cli("render", "--root", self.root)  # every caller of the validate hooks builds vctx the same way
            self.assertEqual(seen[-1]["evidenceIds"], {"ev-1"})
            found = validate.validate_findings(v2_plan(), evidence_ids={"ev-9"})
            self.assertEqual(seen[-1]["evidenceIds"], {"ev-9"}, "an explicit argument wins")
        for bad in ({"evidence.py": "def validate_context(root, plan):\n    return {'x': 1}\n",
                     "records.py": "def validate_context(root, plan):\n    return {'x': 2}\n"},
                    {"evidence.py": "def validate_context(root, plan):\n    return {'team': 1}\n"}):
            with self.subTest(bad=sorted(bad)), extension(bad):
                with self.assertRaises(RuntimeError):
                    validate.validate_findings(v2_plan())
        self.assertIsNotNone(found)

    def test_team_check_still_runs_last(self):
        plan = v1_plan()
        plan["unknowns"] = [{"id": "U-a", "question": "", "severity": "low", "status": "open"}]
        with extension({"unknowns.py": self.UNKNOWNS}):
            errors, _ = validate.validate_plan(plan, team={"agents": []})
        team = [f"roster {r['id']}: not an agent in team.json" for r in plan["roster"]]
        self.assertEqual(errors, ["unknown U-a: needs question, severity and status"] + team)


# ---------------------------------------------------------------------------------------------------- the CLI side

def run_cli(*argv, stdin=None, **env):
    """cli.main in this process: (exit code, stdout, stderr). Capture stays off (temp store)."""
    from bp import cli
    out, err = io.StringIO(), io.StringIO()
    code = 0
    environ = {"APP_BLUEPRINT_NO_CAPTURE": "1", "APP_BLUEPRINT_HOME": S.TEST_STORE, "BLUEPRINT_OWNER": "",
               "BLUEPRINT_GUARD": "block"}
    environ.update(env)
    with mock.patch.dict(os.environ, environ), mock.patch.object(sys, "stdin", io.StringIO(stdin or "")), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main([str(a) for a in argv])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class CliTestBase(TempRoot):
    def setUp(self):
        super().setUp()
        self.plan = v2_plan()
        self.write_plan(self.plan)

    def run_(self, *argv, **kw):
        return run_cli(*argv, "--root", self.root, **kw)


class SubcommandTest(CliTestBase):
    def test_every_2_0_subcommand_exists(self):
        for name in registry.COMMAND_TABLE:
            with self.subTest(command=name):
                code, out, _ = run_cli(name, "--help")
                self.assertEqual(code, 0)
                self.assertIn("--root", out)

    def test_help_keeps_the_1_3_0_order(self):  # review N8
        code, out, _ = run_cli("--help")
        listed = out.split("{", 1)[1].split("}", 1)[0].split(",")
        self.assertEqual(listed, ["init", "validate", "render", "sync", "status", "owner", "claim", "mark", "verify",
                                  "guard", "hook", "contracts", "export", "db-check", "feedback", "report", "doctor",
                                  "migrate", "task", "accept", "evidence", "study"])

    def test_unbuilt_commands_answer_not_built(self):
        for argv, arrives in ((["migrate", "--dry-run"], "W0-B"), (["task", "T-domain", "done", "--by", "owner"], "W1-R"),
                              (["accept", "F-inbox", "--by", "evaluator", "--mode", "browser"], "W3-V"),
                              (["evidence", "list"], "W2-E"), (["study", "source", ".", "--own"], "W2-E")):
            if argv[0] == "migrate" and registry.command_built("migrate"):
                continue  # built since W0-B (tests/test_migrate.py)
            with self.subTest(argv=argv):
                r = S.run_script(S.LAUNCHER, argv + ["--root", self.root], self.root)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"error: {argv[0]} is not built in tool {bp.version.TOOL_VERSION}", r.stderr)
                self.assertIn(arrives, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                code, out, _ = self.run_(*argv, "--json")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["error"]["code"], "not-built")
                self.assertEqual(json.loads(out)["ok"], False)

    def test_a_later_workstream_adds_a_subcommand(self):
        src = '''
            from bp.core import emit

            def register(name, parser):
                parser.add_argument("--dry-run", action="store_true")
                parser.add_argument("--json", action="store_true")
                parser.set_defaults(fn=cmd_accept)

            def cmd_accept(args):
                emit(args, "accept", {"dryRun": args.dry_run}, ["accept ran, dry run: %s" % args.dry_run])
        '''
        with extension({"commands/records.py": src}):
            code, out, err = self.run_("accept", "--dry-run")
            self.assertEqual((code, out), (0, "accept ran, dry run: True\n"), err)
            code, out, _ = self.run_("accept", "--json")
            self.assertEqual(json.loads(out), {"ok": True, "command": "accept", "data": {"dryRun": False}})
            self.assertEqual(self.run_("accept", "--bogus")[0], 2, "argparse usage errors keep exit 2")
            # the migrate hints of 1.7 and 3.3 appear once bp/commands/migrate.py exists (1.5 row d)
            hint = registry.command_built("migrate")
            self.write_plan(v1_plan())
            code, out, _ = self.run_("validate")
            self.assertEqual("To use milestones, features, tasks and unknowns, run: blueprint.py migrate" in out, hint)
            code, out, _ = self.run_("status")
            self.assertEqual("Schema version 1: run migrate to use milestones, features, tasks and unknowns." in out, hint)

    def test_a_missing_lane_inside_a_handler_is_not_built(self):
        src = '''
            def register(name, parser):
                parser.add_argument("lane")
                parser.set_defaults(fn=cmd_study)

            def cmd_study(args):
                import bp.study_lane_that_is_not_here  # noqa: F401
        '''
        with extension({"commands/study.py": src}):
            code, _, err = self.run_("study", "mobile")
        self.assertEqual(code, 1, err)
        self.assertIn("error: ", err)
        self.assertIn("bp.study_lane_that_is_not_here", err)
        self.assertNotIn("Traceback", err)

    def test_a_broken_command_module_breaks_only_its_command(self):
        with extension({"commands/accept_broken.py": "x = 1\n", "commands/records.py": "def register(:\n"}):
            code, out, err = self.run_("status")
            self.assertEqual(code, 0, err)
            code, _, err = self.run_("accept", "F-x")
            self.assertEqual(code, 70, "a broken module is a bug: an incident, not not-built")
            self.assertIn("unexpected error", err)

    def test_usage_errors(self):
        self.assertEqual(self.run_("status", "--bogus")[0], 2)
        code, _, err = self.run_("verify", "--criterion", "C1")
        self.assertEqual(code, 64, err)
        self.assertIn("--feature", err)
        self.assertEqual(self.run_("verify", "--json")[0], 64)


class ContextTest(CliTestBase):
    def test_contributors_run_in_domain_order(self):
        from bp.views.context import build_context
        exts = {
            "unknowns.py": '''
                def context(root, plan, state, ctx):
                    ctx["unknowns"] = [u["id"] for u in plan["unknowns"]]
                    ctx["blocked"] = {"phases": {"P2"}, "milestones": set(), "features": set(), "by": {"phase:P2": ["U-offline"]}}
            ''',
            "features.py": '''
                def context(root, plan, state, ctx):
                    ctx["features"] = [{"id": f["id"], "blockedBy": ctx["unknowns"]} for f in plan["features"]]
            ''',
            "roadmap.py": '''
                def context(root, plan, state, ctx):
                    ctx["milestones"] = [{"id": m["id"], "features": [f["id"] for f in ctx["features"]]} for m in plan["milestones"]]
            '''}
        plan = quiet(bplan.load_plan, self.root)[0]
        state = bplan.load_state(self.root)
        ctx = build_context(self.root, plan, state)
        self.assertEqual(set(ctx), {"root", "plan", "state", "comp", "unknowns", "blocked", "checks", "features",
                                    "milestones", "tasks", "studies"})
        self.assertEqual((ctx["features"], ctx["checks"], ctx["blocked"]["phases"]), ([], {}, set()))
        self.assertEqual([r["path"] for r in ctx["comp"]["rows"]], [f["path"] for f in plan["files"]])
        with extension(exts):
            ctx = build_context(self.root, plan, state)
        self.assertEqual(ctx["milestones"], [{"id": "M1", "features": ["F-log-snag", "F-inbox"]}])
        self.assertEqual(ctx["features"][0]["blockedBy"], ["U-offline"])

    def test_blocking_keys_have_one_format(self):
        from bp.views.context import blocked_by
        ctx = {"blocked": {"by": {"phase:P2": ["U-a"], "milestone:M1": ["U-b"], "feature:F-x": ["U-c"], "P3": ["U-d"]}}}
        self.assertEqual([blocked_by(ctx, k, t) for k, t in (("phase", "P2"), ("milestone", "M1"), ("feature", "F-x"))],
                         [["U-a"], ["U-b"], ["U-c"]])
        self.assertEqual(blocked_by(ctx, "phase", "P3"), [], "a bare id is not a key")
        self.assertEqual(blocked_by({}, "phase", "P2"), [])

    def test_blocked_phases_reach_status_next(self):
        ext = {"unknowns.py": '''
            def context(root, plan, state, ctx):
                ctx["blocked"] = {"phases": {"P1"}, "milestones": set(), "features": set(), "by": {"phase:P1": ["U-offline"]}}
        '''}
        code, out, _ = self.run_("status", "--next")
        self.assertIn("Cargo.toml", out)
        with extension(ext):
            code, out, err = self.run_("status", "--next")
        self.assertEqual(code, 0, err)
        ready = out.split("Ready to build next:")[1]
        self.assertIn("(phase P1 skipped: blocked by U-offline)", ready)
        self.assertIn("apps/web/src/offline.ts", ready)
        self.assertNotIn("Cargo.toml", ready)


class ViewFileTest(CliTestBase):
    ROADMAP = '''
        def views(ctx):
            plan = ctx["plan"]
            if not (plan["milestones"] or plan["tasks"]):
                return {}
            return {"ROADMAP.md": "# %s roadmap\\n%d milestones\\n" % (plan["app"]["name"], len(plan["milestones"]))}
    '''

    def test_a_view_file_is_written_checked_and_removed(self):
        roadmap = self.root / ".blueprint" / "ROADMAP.md"
        with extension({"views/roadmap_md.py": self.ROADMAP}):
            code, out, err = self.run_("render")
            self.assertEqual(code, 0, err)
            self.assertIn("wrote .blueprint/ROADMAP.md", out)
            self.assertEqual(roadmap.read_text(encoding="utf-8"), "# Snag Desk roadmap\n1 milestones\n")
            self.assertEqual(self.run_("render", "--check")[0], 0)
            self.plan["milestones"], self.plan["tasks"] = [], []
            for f in self.plan["features"]:
                f.pop("milestone", None)
            self.write_plan(self.plan)
            code, out, _ = self.run_("render", "--check")
            self.assertEqual(code, 1)
            self.assertIn("ROADMAP.md", out)
            code, out, _ = self.run_("render")
            self.assertIn("removed .blueprint/ROADMAP.md", out)
            self.assertFalse(roadmap.exists())
            self.assertEqual(self.run_("render", "--check")[0], 0)
            self.write_plan(v2_plan())
            self.assertEqual(self.run_("render")[0], 0)
            self.assertTrue(roadmap.exists())
        # the module gone: a view it wrote is no longer wanted
        self.assertEqual(self.run_("render", "--check")[0], 1)
        self.run_("render")
        self.assertFalse(roadmap.exists())

    def test_any_view_name_is_tracked_with_new_files_only(self):
        """Review M4: render records the views it wrote in state.json (generatedViews); one that no module returns
        any more is deleted, and render --check reports it. No list to edit; base views are never deleted."""
        notes = self.root / ".blueprint" / "NOTES-2.md"
        src = "WANT = [True]\ndef views(ctx):\n    return {'NOTES-2.md': 'n\\n'} if WANT[0] else {}\n"
        state_path = self.root / ".blueprint" / "state.json"
        with extension({"views/features_md.py": src}):
            mod = importlib.import_module("bp.views.features_md")
            self.assertEqual(self.run_("render")[0], 0)
            self.assertTrue(notes.exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["generatedViews"], ["NOTES-2.md"])
            mod.WANT[0] = False
            code, out, _ = self.run_("render", "--check")
            self.assertEqual(code, 1)
            self.assertIn("NOTES-2.md", out)
            code, out, _ = self.run_("render")
            self.assertIn("removed .blueprint/NOTES-2.md", out)
            self.assertFalse(notes.exists())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["generatedViews"], [])
            for base in ("STRUCTURE.md", "ROSTER.md", "map.html"):
                self.assertTrue((self.root / ".blueprint" / base).exists())
        hand = self.root / ".blueprint" / "HAND.md"
        hand.write_text("mine", encoding="utf-8")
        self.assertEqual(self.run_("render", "--check")[0], 0, "a file render never wrote is not render's")
        self.assertTrue(hand.exists())
        self.assertFalse(hasattr(registry, "OPTIONAL_VIEWS"))

    def test_record_is_written_before_the_views(self):
        """Review R2-M4: with state.json unwritable, either nothing is written or the orphan is cleaned up later."""
        import stat
        notes = self.root / ".blueprint" / "NOTES-3.md"
        state_path = self.root / ".blueprint" / "state.json"
        self.run_("sync")  # writes state.json and the base views
        src = "WANT = [True]\ndef views(ctx):\n    return {'NOTES-3.md': 'n\\n'} if WANT[0] else {}\n"
        with extension({"views/features_md.py": src}):
            mod = importlib.import_module("bp.views.features_md")
            os.chmod(state_path, stat.S_IREAD)
            try:
                self.run_("render")
            finally:
                os.chmod(state_path, stat.S_IREAD | stat.S_IWRITE)
            mod.WANT[0] = False
            if notes.exists():
                self.assertEqual(self.run_("render", "--check")[0], 1, "an orphan must not look current")
                self.run_("render")
            self.assertFalse(notes.exists())
            self.assertEqual(self.run_("render", "--check")[0], 0)

    def test_reserved_and_hand_made_names(self):
        from bp.views.render import render_all
        with extension({"views/features_md.py": "def views(ctx):\n    return {'README.md': 'x'}\n"}):  # R2-N1
            plan = quiet(bplan.load_plan, self.root)[0]
            with self.assertRaisesRegex(RuntimeError, "features_md.*README.md"):
                render_all(self.root, plan, bplan.load_state(self.root), quiet=True)
        mine = self.root / ".blueprint" / "MINE.md"  # R2-N2: never overwrite a file render did not write
        mine.write_text("hand made", encoding="utf-8")
        src = "def views(ctx):\n    return {'MINE.md': 'generated', 'OTHER.md': 'other'}\n"
        with extension({"views/features_md.py": src}):
            code, out, err = self.run_("render")
        self.assertNotEqual(code, 0)
        self.assertIn("MINE.md", err)
        self.assertIn("features_md", err)
        self.assertIn("move or rename", err)
        self.assertEqual(mine.read_text(encoding="utf-8"), "hand made")
        self.assertTrue((self.root / ".blueprint" / "OTHER.md").exists(), "the other views still render")
        state = json.loads((self.root / ".blueprint" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["generatedViews"], ["OTHER.md"])

    def test_a_v1_render_adds_nothing_to_state(self):
        self.write_plan(v1_plan())
        state_path = self.root / ".blueprint" / "state.json"
        self.run_("sync")
        before = state_path.read_text(encoding="utf-8")
        self.run_("render")
        self.assertEqual(state_path.read_text(encoding="utf-8"), before)
        self.assertNotIn("generatedViews", json.loads(before))

    def test_a_view_hook_may_not_write_outside_blueprint_or_over_a_base_view(self):
        from bp.views.render import render_all
        for bad in ("../x.md", "a/b.md", "STRUCTURE.md", ""):
            with self.subTest(name=bad), extension({"views/features_md.py": f"def views(ctx):\n    return {{{bad!r}: 'x'}}\n"}):
                plan = quiet(bplan.load_plan, self.root)[0]
                with self.assertRaises(RuntimeError):
                    render_all(self.root, plan, bplan.load_state(self.root), quiet=True)


class MapPayloadTest(CliTestBase):
    def render_map(self, tabs=()):
        from bp.views import render
        from bp.views.context import build_context
        plan = quiet(bplan.load_plan, self.root)[0]
        state = bplan.load_state(self.root)
        ctx = build_context(self.root, plan, state)
        with mock.patch.object(render, "tab_scripts", lambda: list(tabs)):
            html = render.render_map(plan, state, ctx["comp"], ctx)
        data = json.loads(html.split('<script id="bp" type="application/json">')[1].split("</script>")[0])
        return html, data

    def test_payload_contributors_and_tab_scripts(self):
        exts = {"features.py": "def map_payload(ctx):\n    return {'features': [{'id': 'F-x'}], 'featureStates': ['draft']}\n",
                "roadmap.py": "def map_payload(ctx):\n    return {'milestones': [{'id': 'M1'}]}\n"}
        tab = "BP_TABS.push({id: 'roadmap', label: function (D) { return 'Roadmap'; }, render: function (host, D, H) {}}); // </script>"
        with extension(exts):
            html, data = self.render_map([tab])
        self.assertEqual((data["features"], data["featureStates"], data["milestones"]), ([{"id": "F-x"}], ["draft"], [{"id": "M1"}]))
        self.assertEqual(data["rows"][1]["features"], ["F-log-snag"])
        self.assertIn("BP_TABS.push({id: 'roadmap'", html)
        self.assertIn("// <\\/script>", html, "tab scripts are escaped like mindmap.js")
        self.assertNotIn("/*__TABS_JS__*/", html)
        self.assertIn("var BP_TABS = [], BP_BRANCHES = [];", html)

    def test_payload_collisions_are_bugs(self):
        for exts in ({"features.py": "def map_payload(ctx):\n    return {'milestones': []}\n",
                      "roadmap.py": "def map_payload(ctx):\n    return {'milestones': []}\n"},
                     {"features.py": "def map_payload(ctx):\n    return {'rows': []}\n"}):
            with self.subTest(exts=sorted(exts)), extension(exts):
                with self.assertRaises(RuntimeError):
                    self.render_map()

    def test_template_has_the_plug_in_point(self):
        tpl = (S.SKILL / "assets" / "map.template.html").read_text(encoding="utf-8")
        self.assertEqual(tpl.count("/*__TABS_JS__*/"), 1)
        self.assertLess(tpl.index("/*__TABS_JS__*/"), tpl.index("var D = JSON.parse"))
        self.assertIn("window.BP_TABS", tpl)
        html, _ = self.render_map()
        self.assertNotIn("/*__TABS_JS__*/", html)
        # mind map branch modes (W1-F's "by feature", 8.4) plug in from mindmap.js, which is inlined after the
        # registries are declared and before the template's script reads them
        self.assertLess(tpl.index("var BP_TABS = [], BP_BRANCHES = [];"), tpl.index("/*__MINDMAP_JS__*/"))
        self.assertIn("window.BP_BRANCHES", tpl)
        for helper in ("fileLeaf: fileLeaf", "patLeaf: patLeaf", "byModule: byModule", "el: el", "stack: stack"):
            self.assertIn(helper, tpl)


class GuardRefusalTest(CliTestBase):
    RECORDS = '''
        def refuse(action, root, plan, path, by):
            if action == "write" and path.startswith(".blueprint/") and path != ".blueprint/blueprint.json":
                return f"{path} is written only by blueprint.py. Use the command instead: blueprint.py sync"
            return None
    '''
    UNKNOWNS = '''
        def refuse(action, root, plan, path, by):
            if action == "claim" and path == "apps/web/src/offline.ts":
                return f"{path} is in phase P2, blocked by unknown U-offline (high, investigating). Resolve it first, or use --force if the lead says so."
            return None
    '''

    def hook_pre(self, rel, **env):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / rel)}})
        return run_cli("hook", "pre", stdin=payload, **env)

    def test_write_refusal_reaches_guard_and_hook_pre(self):
        self.assertEqual(self.run_("guard", ".blueprint/state.json")[0], 0, "1.x allows every .blueprint path")
        with extension({"records.py": self.RECORDS}):
            code, _, err = self.run_("guard", ".blueprint/state.json")
            self.assertEqual(code, 2)
            self.assertIn(".blueprint/state.json is written only by blueprint.py", err)
            self.assertEqual(self.run_("guard", ".blueprint/blueprint.json")[0], 0)
            self.assertEqual(self.run_("guard", "Cargo.toml", "--by", "cto-architect")[0], 0)
            code, _, err = self.hook_pre(".blueprint/state.json")
            self.assertEqual(code, 2)
            self.assertIn("written only by blueprint.py", err)
            code, out, _ = self.hook_pre(".blueprint/state.json", BLUEPRINT_GUARD="ask")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
            self.assertEqual(self.hook_pre(".blueprint/state.json", BLUEPRINT_GUARD="off")[0], 0)
            self.assertEqual(self.hook_pre("scratch/new.py")[0], 2, "the 1.x guard still decides first")

    def test_a_crashing_refuse_hook_fails_closed(self):
        with extension({"records.py": "def refuse(action, root, plan, path, by):\n    raise KeyError('bug')\n"}):
            code, _, err = self.run_("guard", "Cargo.toml")
            self.assertEqual(code, 2, err)
            self.assertEqual(self.hook_pre("Cargo.toml")[0], 2)

    def test_claim_refusal_exits_3_unless_forced(self):
        with extension({"unknowns.py": self.UNKNOWNS}):
            code, _, err = self.run_("claim", "apps/web/src/offline.ts", "--by", "frontend-engineer")
            self.assertEqual(code, 3, err)
            self.assertIn("blocked by unknown U-offline", err)
            self.assertEqual(self.run_("claim", "apps/web/src/Inbox.tsx", "--by", "frontend-engineer")[0], 0)
            self.assertIn("next: blueprint.py status --unknowns", err)  # review N5: a refusal names the next step
            self.assertEqual(self.run_("claim", "apps/web/src/offline.ts", "--by", "frontend-engineer", "--force")[0], 0)
        self.assertEqual(self.run_("claim", "apps/web/src/offline.ts", "--by", "frontend-engineer")[0], 0)
        tuple_src = ("def refuse(action, root, plan, path, by):\n"
                     "    return ('no: ' + path, 'owner-busy', 'ask the lead') if action == 'claim' else None\n")
        with extension({"roadmap.py": tuple_src}):
            code, _, err = self.run_("claim", "apps/web/src/Inbox.tsx", "--by", "frontend-engineer")
        self.assertEqual(code, 3)
        self.assertIn("error: no: apps/web/src/Inbox.tsx", err)
        self.assertIn("next: ask the lead", err)


LOAD_ERRORS = {  # kind -> blueprint.json text that cannot load
    "bad-json": "{",
    "not-an-object": "[]",
    "shape": json.dumps(dict(v1_plan(), files="x")),
    "version-missing": json.dumps({k: v for k, v in v1_plan().items() if k != "version"}),
    "version-string": json.dumps(dict(v1_plan(), version="1")),
    "version-bool": json.dumps(dict(v1_plan(), version=True)),
    "version-zero": json.dumps(dict(v1_plan(), version=0)),
    "too-new": json.dumps(dict(v1_plan(), version=3)),
    "mistyped-2.0-key": json.dumps(dict(v1_plan(), features="F-x")),
}
REPAIR = ("Writes are refused until the plan loads. Fix .blueprint/blueprint.json, then run: "
          "python .blueprint/bin/blueprint.py validate")


class GuardLoadFailureTest(CliTestBase):
    """Review M1: guard and hook pre fail closed (exit 2) when a blueprint exists but cannot load, except a write to
    .blueprint/blueprint.json itself, so the plan can be repaired."""

    def hook_pre(self, rel):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / rel)}})
        return run_cli("hook", "pre", stdin=payload)

    def test_every_load_error_refuses_writes_but_the_plan_itself(self):
        for kind, text in LOAD_ERRORS.items():
            with self.subTest(kind=kind):
                (self.root / ".blueprint" / "blueprint.json").write_text(text, encoding="utf-8")
                for call in (lambda p: self.run_("guard", p), self.hook_pre):
                    code, _, err = call("random/new.txt")
                    self.assertEqual(code, 2, err)
                    self.assertIn(REPAIR, err)
                    self.assertTrue(err.strip().splitlines()[0], "the load error comes first")
                    self.assertNotIn("Traceback", err)
                    for other in (".blueprint/state.json", ".blueprint/blueprint.json.bak", ".blueprint/bin/blueprint.py",
                                  "blueprint.json"):
                        self.assertEqual(call(other)[0], 2, other)
                    self.assertEqual(call(".blueprint/blueprint.json")[0], 0, "the repair write is allowed")
                    self.assertEqual(call(".blueprint/./blueprint.json")[0], 0)

    def test_no_blueprint_behaves_as_before(self):
        (self.root / ".blueprint" / "blueprint.json").unlink()
        self.assertEqual(self.run_("guard", "random/new.txt")[0], 1)
        self.assertEqual(self.hook_pre("random/new.txt")[0], 0)


class UnreadablePlanTest(CliTestBase):
    """Review R2-M1: a blueprint.json that cannot even be read (bad encoding, too deep, a folder) also makes guard and
    hook pre refuse (exit 2) and validate --json answer bad-json; a crash while loading refuses too (incident)."""

    def hook_pre(self, rel):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / rel)}})
        return run_cli("hook", "pre", stdin=payload)

    def make(self, kind):
        path = self.root / ".blueprint" / "blueprint.json"
        if path.is_dir():
            path.rmdir()
        elif path.exists():
            path.unlink()
        if kind == "non-utf8":
            path.write_bytes(b'{"version": 1, "app": {"name": "Caf\xe9"}}')
        elif kind == "deep":
            path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        else:
            path.mkdir()

    def test_unreadable_plans(self):
        for kind in ("non-utf8", "deep", "folder"):
            with self.subTest(kind=kind):
                self.make(kind)
                for call in (lambda p: self.run_("guard", p), self.hook_pre):
                    code, _, err = call("random/new.txt")
                    self.assertEqual(code, 2, err)
                    self.assertIn(REPAIR, err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(call(".blueprint/state.json")[0], 2)
                code, out, err = self.run_("validate", "--json")
                self.assertEqual(code, 1, err)
                self.assertEqual(json.loads(out)["error"]["code"], "bad-json")

    def test_a_crash_while_loading_refuses_and_is_an_incident(self):
        from bp.commands import progress
        with mock.patch.object(progress, "load_plan", side_effect=KeyError("boom")):
            code, _, err = self.run_("guard", "random/new.txt")
            self.assertEqual(code, 2, err)
            self.assertIn("unexpected error", err)
            self.assertIn(REPAIR, err)
            self.assertEqual(self.run_("guard", ".blueprint/blueprint.json")[0], 0, "the repair write stays allowed")


class GuardModeTest(CliTestBase):
    """Review R2-N4: an unknown BLUEPRINT_GUARD value means block, with one line naming it and the accepted values."""

    def test_unknown_mode_blocks(self):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": str(self.root / "random" / "x.txt")}})
        for text in (V2_EXAMPLE_TEXT, "{"):
            (self.root / ".blueprint" / "blueprint.json").write_text(text, encoding="utf-8")
            code, _, err = run_cli("hook", "pre", stdin=payload, BLUEPRINT_GUARD="bogus")
            self.assertEqual(code, 2, err)
            self.assertIn("BLUEPRINT_GUARD='bogus' is not one of block, ask, off", err)


class JsonLoadFailureTest(CliTestBase):
    """Review M2: with --json, a plan that cannot load (or no blueprint) prints one failure envelope on stdout, with
    the same exit code as without --json; the human message stays on stderr."""

    CALLS = (["validate"], ["status"], ["verify", "--feature", "F-x"], ["export", "agenticteam"])

    def test_envelopes(self):
        cases = dict(LOAD_ERRORS, **{"no-blueprint": None})
        want = {"bad-json": "bad-json", "not-an-object": "bad-shape", "shape": "bad-shape", "version-missing": "bad-shape",
                "version-string": "bad-shape", "version-bool": "bad-shape", "version-zero": "bad-shape",
                "too-new": "schema-too-new", "mistyped-2.0-key": "bad-shape", "no-blueprint": "no-blueprint"}
        for kind, text in cases.items():
            path = self.root / ".blueprint" / "blueprint.json"
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text, encoding="utf-8")
            for argv in self.CALLS:
                with self.subTest(kind=kind, argv=argv):
                    plain = self.run_(*argv)
                    code, out, err = self.run_(*argv, "--json")
                    self.assertEqual(code, plain[0])
                    self.assertEqual(code, 1)
                    env = json.loads(out)
                    self.assertEqual((env["ok"], env["error"]["code"]), (False, want[kind]))
                    self.assertTrue(env["error"]["message"])
                    self.assertTrue(env["error"]["next"])
                    self.assertTrue(err.strip(), "the human message stays on stderr")


class GuardPathTest(CliTestBase):
    """Review M3: guard normalises the path exactly as hook pre does, before guard_decision and the refuse hooks."""

    RECORDS = '''
        def refuse(action, root, plan, path, by):
            if action == "write" and path == ".blueprint/state.json":
                return f"{path} is written only by blueprint.py"
    '''

    def hook_pre(self, raw):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": raw}})
        return run_cli("hook", "pre", stdin=payload)

    def test_every_spelling_of_a_protected_path_is_refused(self):
        (self.root / ".blueprint" / "state.json").write_text("{}", encoding="utf-8")
        forms = [str(self.root / ".blueprint" / "state.json"), ".blueprint//state.json", ".blueprint/./state.json",
                 "crates/../.blueprint/state.json", ".blueprint\\state.json" if os.name == "nt" else ".blueprint/state.json"]
        if os.name == "nt":
            forms += [str(self.root / ".blueprint" / "state.json").replace("\\", "/"), ".BLUEPRINT/State.JSON",
                      str(self.root).upper() + "\\.blueprint\\state.json"]
        link = self.root / "linked"
        try:
            os.symlink(self.root / ".blueprint", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name == "nt":
                import subprocess
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.root / ".blueprint")], capture_output=True)
        if link.exists():
            forms.append("linked/state.json")
        with extension({"records.py": self.RECORDS}):
            for raw in forms:
                with self.subTest(path=raw):
                    code, _, err = self.run_("guard", raw)
                    self.assertEqual(code, 2, err)
                    self.assertIn("written only by blueprint.py", err)
                    absolute = raw if os.path.isabs(raw) else str(self.root / raw)
                    self.assertEqual(self.hook_pre(absolute)[0], 2)
            self.assertEqual(self.run_("guard", "crates/../Cargo.toml", "--by", "cto-architect")[0], 0)
            self.assertEqual(self.run_("guard", "./Cargo.toml", "--by", "backend-lead")[0], 2, "owner check on the normal form")
        self.assertEqual(self.run_("guard", "../outside.txt")[0], 2, "outside the project: unplanned, as in 1.x")
        self.assertEqual(self.hook_pre(str(self.root.parent / "outside.txt"))[0], 0, "hook pre ignores outside paths, as in 1.x")


@unittest.skipUnless(os.name == "nt", "Windows path spellings")
class WindowsPathSpellingTest(CliTestBase):
    """Review R2-M3: \\\\?\\ and UNC spellings of a path inside the project normalise to the same rel; a name that does
    not exist yet loses the trailing dots and spaces Windows drops; refuse hooks compare with core.path_key."""

    RECORDS = '''
        from bp.core import path_key
        def refuse(action, root, plan, path, by):
            if action == "write" and path_key(path) in (path_key(".blueprint/state.json"), path_key(".blueprint/ROADMAP.md")):
                return f"{path} is written only by blueprint.py"
    '''

    def spellings(self, rel):
        full = str((self.root / rel).resolve())
        out = ["\\\\?\\" + full]
        unc = "\\\\localhost\\" + full[0] + "$" + full[2:]
        try:
            if os.path.exists("\\\\localhost\\" + full[0] + "$\\"):
                out.append(unc)
        except OSError:
            pass
        return out

    def hook_pre(self, raw):
        payload = json.dumps({"cwd": str(self.root), "tool_input": {"file_path": raw}})
        return run_cli("hook", "pre", stdin=payload)

    def test_protected_and_unplanned_paths(self):
        (self.root / ".blueprint" / "state.json").write_text("{}", encoding="utf-8")
        with extension({"records.py": self.RECORDS}):
            for raw in self.spellings(".blueprint/state.json"):
                with self.subTest(raw=raw):
                    self.assertEqual(self.run_("guard", raw)[0], 2)
                    self.assertEqual(self.hook_pre(raw)[0], 2)
            for raw in self.spellings("random/new.txt"):
                with self.subTest(raw=raw):
                    self.assertEqual(self.run_("guard", raw)[0], 2)
                    self.assertEqual(self.hook_pre(raw)[0], 2)
            for raw in (".blueprint/roadmap.md.", ".blueprint/ROADMAP.md .", ".blueprint\\RoadMap.md"):
                with self.subTest(raw=raw):
                    code, _, err = self.run_("guard", raw)
                    self.assertEqual(code, 2, err)
                    self.assertIn("written only by blueprint.py", err)

    def test_project_path_and_path_key(self):
        from bp import core
        self.assertEqual(core.project_path(self.root, ".blueprint/roadmap.md. "), ".blueprint/roadmap.md")
        self.assertEqual(core.project_path(self.root, "\\\\?\\" + str(self.root.resolve()) + "\\a\\b.txt"), "a/b.txt")
        self.assertIsNone(core.project_path(self.root, "\\\\?\\" + str(self.root.resolve().parent) + "\\x.txt"))
        self.assertEqual(core.path_key("A\\B/C.md"), "a/b/c.md")
        from bp.commands import progress
        self.assertIs(progress.project_path, core.project_path)

    def test_repair_exception_on_the_long_spelling(self):  # review R2-N3
        (self.root / ".blueprint" / "blueprint.json").write_text("{", encoding="utf-8")
        raw = "\\\\?\\" + str((self.root / ".blueprint" / "blueprint.json").resolve())
        self.assertEqual(self.run_("guard", raw)[0], 0)
        self.assertEqual(self.hook_pre(raw)[0], 0)
        self.assertEqual(self.run_("guard", ".BLUEPRINT\\Blueprint.JSON")[0], 0)


class PathKeyTest(unittest.TestCase):
    def test_path_key(self):
        from bp import core
        self.assertEqual(core.path_key("a\\b/c.md"), os.path.normcase("a/b/c.md").replace("\\", "/"))


class VerifyFeatureAndStatusCardTest(CliTestBase):
    def test_not_built_until_the_owner_module_exists(self):
        for argv, arrives in ((["verify", "--feature", "F-inbox", "--run"], "W3-V"), (["status", "--feature", "F-inbox"], "W1-F"),
                              (["status", "--features"], "W1-F"), (["status", "--milestone", "M1"], "W1-R"),
                              (["status", "--tasks"], "W1-R"), (["status", "--unknowns"], "W1-U")):
            with self.subTest(argv=argv):
                code, _, err = self.run_(*argv)
                self.assertEqual(code, 1, err)
                self.assertIn("is not built", err)
                self.assertIn(arrives, err)

    def test_verify_feature_hook(self):
        src = '''
            CALLS = []
            RESULT = ([{"kind": "check", "criterion": "C1", "result": "pass"}], 0)

            def verify_feature(args, ctx):
                CALLS.append((args.feature, args.criterion, args.repeat, args.by, args.run, sorted(ctx)))
                if not args.json:
                    print("C1 pass (integration, 1/1)")
                return RESULT
        '''
        with extension({"records.py": src}):
            mod = importlib.import_module("bp.records")
            code, out, err = self.run_("verify", "--feature", "F-log-snag", "--criterion", "C1", "--repeat", "3",
                                       "--by", "backend-lead", "--run")
            self.assertEqual((code, out), (0, "C1 pass (integration, 1/1)\n"), err)
            self.assertEqual(mod.CALLS[0][:5], ("F-log-snag", "C1", 3, "backend-lead", True))
            self.assertIn("checks", mod.CALLS[0][5])
            code, out, _ = self.run_("verify", "--feature", "F-log-snag", "--run", "--json")
            self.assertEqual(json.loads(out), {"ok": True, "command": "verify", "data": {"records": mod.RESULT[0]}})
            for result in (([{"kind": "check", "result": "timeout"}], 0), ([{"kind": "check", "result": "pass"}], "0"),
                           ([{"kind": "check", "result": "skipped"}], 0), ([{"kind": "check", "result": "pass"}], 1)):
                with self.subTest(result=result):
                    mod.RESULT = result
                    self.assertEqual(self.run_("verify", "--feature", "F-log-snag", "--run")[0], 1, "fail closed")
            self.assertEqual(self.run_("verify", "--feature", "F-x", "--repeat", "0")[0], 64)

    def test_status_card_and_status_hooks(self):
        exts = {"features.py": '''
            def status_card(ctx, fid):
                return ["card for " + fid, "files: %d" % len(ctx["comp"]["rows"])]

            def status_lines(ctx, args):
                return ["features: %d%s" % (len(ctx["plan"]["features"]), " (listing)" if args.features else "")]

            def status_json(ctx):
                return {"features": [{"id": f["id"]} for f in ctx["plan"]["features"]]}
        ''', "unknowns.py": '''
            def status_lines(ctx, args):
                return ["open questions: 1"]

            def status_json(ctx):
                return {"unknowns": [{"id": "U-offline"}]}
        '''}
        with extension(exts):
            code, out, err = self.run_("status", "--feature", "F-inbox")
            self.assertEqual((code, out), (0, "card for F-inbox\nfiles: 5\n"), err)
            code, out, _ = self.run_("status", "--features")
            lines = out.rstrip().splitlines()
            self.assertEqual(lines[-2:], ["open questions: 1", "features: 2 (listing)"])
            self.write_plan(dict(v1_plan(), features=[], unknowns=[]))
            code, out, _ = self.run_("status")
            self.assertTrue(out.rstrip().splitlines()[-1].startswith("Schema version 1"), "the schema line ends status")
            self.write_plan(self.plan)
            for argv in (["status", "--json"], ["export", "json"]):
                code, out, err = self.run_(*argv)
                data = json.loads(out)
                self.assertEqual((data["features"], data["unknowns"], data["milestones"], data["tasks"]),
                                 ([{"id": "F-log-snag"}, {"id": "F-inbox"}], [{"id": "U-offline"}], [], []), argv)
        with extension({"features.py": "def status_json(ctx):\n    return {'rows': []}\n"}):
            code, _, err = self.run_("status", "--json")
        self.assertEqual(code, 70, "a hook overwriting a 1.x key is a bug")


class StructureAndRosterHooksTest(CliTestBase):
    def views(self, plan_dict, exts=None):
        from bp.views.context import build_context
        from bp.views.roster import render_roster
        from bp.views.structure import render_structure
        self.write_plan(plan_dict)
        plan = quiet(bplan.load_plan, self.root)[0]
        state = bplan.load_state(self.root)
        with extension(exts or {}):
            ctx = build_context(self.root, plan, state)
            return (render_structure(plan, state, ctx["comp"], ctx), render_roster(plan, state, ctx["comp"], ctx),
                    render_structure(plan, state, ctx["comp"]), render_roster(plan, state, ctx["comp"]))

    def test_no_hooks_no_change(self):
        for plan in (v1_plan(), v2_plan()):
            s_ctx, r_ctx, s, r = self.views(plan)
            self.assertEqual((s_ctx, r_ctx), (s, r))

    def test_file_tags_and_roster_sections(self):
        exts = {"features.py": '''
            def file_tags(ctx):
                out = {}
                for f in ctx["plan"]["files"]:
                    tags = list(f.get("features", [])) + (["infra"] if f.get("infrastructure") else [])
                    if tags:
                        out[f["path"]] = tags
                return out

            def roster_sections(ctx):
                return [("Who may accept what", "| Feature | Independent |\\n|---|---|\\n| F-inbox | evaluator |\\n")]
        ''', "roadmap.py": '''
            def file_tags(ctx):
                return {"apps/web/src/offline.ts": ["T-domain"]}
        '''}
        s, r, _, plain_r = self.views(v2_plan(), exts)
        self.assertIn("main.rs  ○ POST /snags and GET /snags (backend-lead, P1) [F-log-snag]", s)
        self.assertIn("Cargo.toml  ○ Workspace manifest (cto-architect, P1) [infra]", s)
        self.assertIn("offline.ts  ○ Offline cache for the inbox (frontend-engineer, P2) [F-inbox] [T-domain]", s)
        self.assertTrue(r.startswith(plain_r))
        self.assertEqual(r[len(plain_r):], "## Who may accept what\n\n| Feature | Independent |\n|---|---|\n| F-inbox | evaluator |\n\n")

    def test_stale_line_in_structure_when_features_exist(self):
        self.touch("Cargo.toml", "a\n")
        st = {"stateVersion": 2, "tasks": {}, "files": {"Cargo.toml": {"verified": {"ok": True, "cmd": "c", "sha": "x",
                                                                               "inputs": {"Cargo.toml": "sha256:" + "0" * 64}}}},
              "patterns": {}, "unplanned": []}
        (self.root / ".blueprint" / "state.json").write_text(json.dumps(st), encoding="utf-8")
        s, _, _, _ = self.views(v2_plan())
        self.assertIn("↻ 1 file(s) need re-verifying (inputs changed): verify --stale --run", s)
        s, _, _, _ = self.views(v1_plan())
        self.assertNotIn("↻", s)


class SignalsTest(CliTestBase):
    SIGNALS = "import json\ndef record(root, signal, code):\n    (root / 'signals.txt').open('a').write(signal + ' ' + code + '\\n')\n"

    def test_guard_and_verify_send_signals(self):
        for m in self.plan["modules"]:
            m["verify"] = f'"{S.PY}" -c "import sys; sys.exit(3)"'
        self.write_plan(self.plan)
        self.touch("Cargo.toml", "x\n")
        with extension({"signals.py": self.SIGNALS}):
            self.run_("guard", "scratch/x.py")
            self.run_("guard", "Cargo.toml", "--by", "backend-lead")
            self.run_("verify", "--run")
        self.assertEqual((self.root / "signals.txt").read_text().splitlines(),
                         ["guard guard-unplanned", "guard guard-wrong-owner", "verify verify-fail"])


class EndToEndExtensionTest(unittest.TestCase):
    """A temp copy of the skill gains a later workstream's files (modules and a tab script); the real CLI, run as a
    process from that copy, picks every one of them up. Nothing of W0-A2 is edited."""

    FILES = {
        "scripts/bp/features.py": '''
            CODES = [(r"^feature \\S+: needs title", "feature-incomplete")]

            def validate(plan, E, W, vctx):
                for f in plan["features"]:
                    if not f.get("title"):
                        E("feature %s: needs title, statement, kind and owner" % f["id"], entry="feature " + f["id"])

            def status_card(ctx, fid):
                return ["%s: planned" % fid]

            def map_payload(ctx):
                return {"features": [{"id": f["id"]} for f in ctx["plan"]["features"]]}
        ''',
        "scripts/bp/records.py": '''
            def refuse(action, root, plan, path, by):
                if action == "write" and path == ".blueprint/state.json":
                    return ".blueprint/state.json is written only by blueprint.py"
        ''',
        "scripts/bp/views/features_md.py": '''
            def views(ctx):
                return {"FEATURES.md": "# Features\\n"} if ctx["plan"]["features"] else {}
        ''',
        "assets/tab-features.js": "BP_TABS.push({id: 'features', label: function (D) { return 'Features (' + D.features.length + ')'; }, "
                                  "render: function (host, D, H) { host.appendChild(H.el('p', {text: 'x'})); }});\n",
    }

    def test_later_files_are_picked_up_by_the_real_cli(self):
        base = Path(tempfile.mkdtemp(prefix="bp e2e ext "))
        self.addCleanup(shutil.rmtree, base, True)
        skill = base / "skill"
        shutil.copytree(S.SCRIPTS, skill / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(S.SKILL / "assets", skill / "assets")
        for rel, src in self.FILES.items():
            (skill / rel).write_text(textwrap.dedent(src), encoding="utf-8")
        launcher = skill / "scripts" / "blueprint.py"
        root = base / "project"
        root.mkdir()
        r = S.run_script(launcher, ["init", "--name", "E2E", "--root", root], base)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((root / ".blueprint" / "bin" / "bp" / "features.py").is_file())
        self.assertTrue((root / ".blueprint" / "bin" / "tab-features.js").is_file())
        plan = v2_plan()
        plan["features"][1]["title"] = ""
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan), encoding="utf-8")
        copy_ = root / ".blueprint" / "bin" / "blueprint.py"
        r = S.run_script(copy_, ["validate", "--json"], root)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual([(f["code"], f["entry"]) for f in json.loads(r.stdout)["data"]["errors"]],
                         [("feature-incomplete", "feature F-inbox")])
        plan["features"][1]["title"] = "Review snags in the inbox"
        (root / ".blueprint" / "blueprint.json").write_text(json.dumps(plan), encoding="utf-8")
        r = S.run_script(copy_, ["render"], root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wrote .blueprint/FEATURES.md", r.stdout)
        html = (root / ".blueprint" / "map.html").read_text(encoding="utf-8")
        self.assertIn("BP_TABS.push({id: 'features'", html)
        self.assertIn('"features": [{"id": "F-log-snag"}, {"id": "F-inbox"}]', html)
        r = S.run_script(copy_, ["status", "--feature", "F-inbox"], root)
        self.assertEqual((r.returncode, r.stdout), (0, "F-inbox: planned\n"), r.stderr)
        r = S.run_script(copy_, ["guard", ".blueprint/state.json"], root)
        self.assertEqual(r.returncode, 2, r.stderr)
        r = S.run_script(copy_, ["accept"], root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not built", r.stderr)
        self.assertEqual([str(p) for p in skill.rglob("__pycache__")] + [str(p) for p in root.rglob("__pycache__")], [])


if __name__ == "__main__":
    unittest.main()
