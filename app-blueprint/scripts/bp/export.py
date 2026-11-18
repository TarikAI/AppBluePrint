"""export: moved from blueprint.py 1.3.0 (contract 1.5)."""

from bp.plan import WRITE_ACCESS


def shq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def agenticteam_tasks(plan, comp, team=None):
    writable = None
    if team is not None:
        writable = {a["id"] for a in team.get("agents", []) if a.get("access") in WRITE_ACCESS}
    tasks, by_phase = [], {}
    group_of = {}
    for ph in plan["phases"]:
        owners = sorted(({r["owner"] for r in comp["rows"] if r["phase"] == ph["id"] and r["status"] != "verified"} |
                         {p["owner"] for p in comp["patterns"] if p["phase"] == ph["id"]}) - {None})
        for o in owners:
            tid = f"bp-{ph['id']}-{o}".lower()
            for r in comp["rows"]:
                if r["phase"] == ph["id"] and r["owner"] == o:
                    group_of[r["path"]] = tid
    prev_ids = []
    for ph in plan["phases"]:
        ids_this = []
        owners = sorted(({r["owner"] for r in comp["rows"] if r["phase"] == ph["id"] and r["status"] != "verified"} |
                         {p["owner"] for p in comp["patterns"] if p["phase"] == ph["id"]}) - {None})
        for o in owners:
            tid = f"bp-{ph['id']}-{o}".lower()
            files = [r for r in comp["rows"] if r["phase"] == ph["id"] and r["owner"] == o and r["status"] != "verified"]
            pats = [p for p in comp["patterns"] if p["phase"] == ph["id"] and p["owner"] == o]
            deps = set(prev_ids)
            for r in files:
                for d in r["dependsOn"]:
                    g = group_of.get(d)
                    if g and g != tid:
                        deps.add(g)
            checks = sorted({r["verify"] for r in files if r["verify"]} | {p["verify"] for p in pats if p["verify"]})
            tasks.append({
                "id": tid, "owner": o, "stage": "04_build", "phase": ph["id"], "action": "build", "target": "local",
                "title": f"{ph['id']} {ph.get('name', '')}: {o} builds {len(files)} file(s)" + (f" and {len(pats)} pattern(s)" if pats else ""),
                "paths": [r["path"] for r in files] + [p["glob"] for p in pats],
                "dependsOn": sorted(deps),
                "inputs": [".blueprint/STRUCTURE.md", ".blueprint/ROSTER.md"],
                "acceptance": [f"Every listed file exists, matches its purpose in .blueprint/STRUCTURE.md, and is marked built",
                               f"Phase exit: {ph.get('exit') or ph.get('goal') or ph['id']}"],
                "checks": checks,
                "writable": None if writable is None else (o in writable),
            })
            ids_this.append(tid)
        if ids_this:
            tasks.append({"id": f"bp-{ph['id']}-verify".lower(), "owner": "qa-lead", "stage": "04_build", "phase": ph["id"],
                          "action": "test", "target": "local",
                          "title": f"{ph['id']} {ph.get('name', '')}: verify the phase", "paths": [], "dependsOn": ids_this,
                          "inputs": [".blueprint/STRUCTURE.md"], "verifies": ids_this,
                          "acceptance": [f"python .blueprint/bin/blueprint.py verify --phase {ph['id']} --run passes"],
                          "checks": [f"python .blueprint/bin/blueprint.py verify --phase {ph['id']} --run"], "writable": None})
            prev_ids = ids_this + [f"bp-{ph['id']}-verify".lower()]
    return tasks
