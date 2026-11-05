"""Each stack preset must turn into a blueprint that validates: every phase-1 file, contract and
pattern has exactly one owner, owner rules only partly overlap where marked override, modules exist.
Run: python -m unittest discover -s tests -v"""
import importlib.util
import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
STACKS = SKILL / "references" / "stacks"
spec = importlib.util.spec_from_file_location("blueprint", SKILL / "scripts" / "blueprint.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

REVIEW_ONLY = {"security-engineer", "qa-lead", "code-reviewer", "performance-engineer", "accessibility-specialist",
               "privacy-engineer", "fraud-risk-analyst", "finops-engineer", "compliance-advisor", "system-mapper", "growth-analyst"}


def section_table(text, heading):
    m = re.search(r"^## " + re.escape(heading) + r".*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[\s\-:|]+\|$", line):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        rows.append(cells)
    return rows[1:]  # drop the header row


def clean_path(cell):
    m = re.search(r"`([^`]+)`", cell)
    p = (m.group(1) if m else cell).strip()
    return p.split(" ")[0]


def preset_to_plan(text):
    modules = []
    for cells in section_table(text, "Modules and layers"):
        mid, path, layer, deps = cells[0], clean_path(cells[1]), cells[2], cells[3]
        dep_ids = [d.strip() for d in re.split(r"[,;]", re.sub(r"\(.*?\)", "", deps)) if d.strip() and d.strip().lower() != "none"]
        modules.append({"id": mid, "path": path.rstrip("/") if "/" in path else "", "layer": layer,
                        "purpose": layer or mid, "dependsOn": dep_ids, "verify": "true"})
    ids = {m["id"] for m in modules}
    for m in modules:  # keep only ids that exist; dependency names in presets are prose
        m["dependsOn"] = [d for d in m["dependsOn"] if d in ids]

    def guess_module(path):
        best = None
        for m in modules:
            if m["path"] and (path == m["path"] or path.startswith(m["path"] + "/")):
                if best is None or len(m["path"]) > len(best["path"]):
                    best = m
        return (best or modules[-1])["id"]

    files, seen = [], set()
    for cells in section_table(text, "Phase 1 walking skeleton"):
        path = clean_path(cells[0])
        if path in seen:
            continue
        seen.add(path)
        files.append({"path": path, "action": cells[1] if cells[1] in bp.ACTIONS else "create",
                      "module": cells[2], "phase": "P1", "purpose": cells[3] or path, "verify": "true"})
    for cells in section_table(text, "Shared contracts"):
        path = clean_path(cells[0])
        if path in seen:
            continue
        seen.add(path)
        files.append({"path": path, "action": "create", "module": guess_module(path), "phase": "P2",
                      "purpose": cells[1] or path, "contract": True, "verify": "true"})
    patterns = []
    for cells in section_table(text, "Repeated-file patterns"):
        glob = clean_path(cells[1])
        patterns.append({"id": cells[0], "glob": glob, "module": guess_module(glob), "phase": "P2",
                         "purpose": cells[2] or cells[0], "instance": cells[2] or cells[0], "verify": "true"})
    owners, roster = [], {}
    for cells in section_table(text, "Suggested owner rules"):
        pattern, owner, note = clean_path(cells[1]), cells[2], cells[3] if len(cells) > 3 else ""
        owners.append({"pattern": pattern, "owner": owner, "override": "override" in note.lower()})
        roster[owner] = {"id": owner, "kind": "agent", "harness": "agenticteam"}
    return {"version": 1, "app": {"name": "preset"}, "stack": [], "modules": modules,
            "phases": [{"id": "P1", "name": "Walking skeleton"}, {"id": "P2", "name": "Later"}],
            "files": files, "patterns": patterns, "owners": owners, "roster": list(roster.values()),
            "decisions": [], "ignore": [], "approach": {}}


class PresetTest(unittest.TestCase):
    def test_presets_exist(self):
        names = sorted(p.stem for p in STACKS.glob("*.md"))
        self.assertEqual(names, ["nextjs-prisma", "pnpm-monorepo-docker", "rust-agent-engine", "vite-react-supabase"])

    def test_each_preset_validates(self):
        for path in sorted(STACKS.glob("*.md")):
            with self.subTest(preset=path.stem):
                text = path.read_text(encoding="utf-8")
                for heading in ("Stack", "Layout", "Modules and layers", "Phase 1 walking skeleton", "Shared contracts",
                                "Repeated-file patterns", "Ignore", "Verify commands", "Suggested owner rules", "Pitfalls"):
                    self.assertRegex(text, r"(?m)^## " + re.escape(heading), f"missing section {heading}")
                plan = preset_to_plan(text)
                self.assertGreaterEqual(len(plan["files"]), 8)
                errors, _ = bp.validate_plan(plan)
                self.assertEqual(errors, [], "\n".join(errors))
                owners = {r["owner"] for r in plan["owners"]}
                self.assertFalse(owners & REVIEW_ONLY, f"review-only roles own files: {owners & REVIEW_ONLY}")


if __name__ == "__main__":
    unittest.main()
