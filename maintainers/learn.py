#!/usr/bin/env python3
"""learn.py: the maintainers' side of the app-blueprint learning loop.

Users' runs of blueprint.py leave records (incidents, feedback, friction) in a local store that
lives outside every project. This script turns those records, and open GitHub issues, into
observations; groups observations into fix candidates; records the owner's decisions; checks
files for private data before they are published; exports a sanitized bundle of lessons; syncs
the store between the owner's machines; and lists the published stack presets.

The contract (store layout, record formats, grouping keys, promotion rule, commands) is
maintainers/SPEC.md. Standard library only, Python 3.10+.

Exit codes: 0 ok, 1 findings or errors, 64 usage.
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

LEARN_VERSION = "1.0.0"
REPO_ROOT = Path(__file__).resolve().parent.parent
STACKS_REL = Path("app-blueprint") / "references" / "stacks"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 64

INCIDENTS = "incidents.jsonl"
FEEDBACK = "feedback.jsonl"
FRICTION = "friction.jsonl"
OBSERVATIONS = "observations.jsonl"
DECISIONS_FILE = "decisions.jsonl"
SALT = "salt"
PRIVATE_TERMS = "private-terms.txt"
STORE_FILES = (INCIDENTS, FEEDBACK, FRICTION, OBSERVATIONS, DECISIONS_FILE)

BUNDLE_FORMAT = "app-blueprint-lessons/1"
DEFAULT_REPO = "TarikAI/AppBluePrint"
DEFAULT_MIN_PROJECTS = 2
LOCAL_SOURCES = ("incident", "feedback", "friction")
DECISIONS = ("adopt", "reject", "defer")
ISSUE_PROJECT = "gh-issue"
SUMMARY_MAX = 200
ISSUE_BODY_MAX = 4000
SCAN_MAX_BYTES = 2 * 1024 * 1024
SCAN_SKIP_DIRS = {".git", "__pycache__"}
ALLOW_MARK = "scan-private: allow"
CANDIDATE_RE = re.compile(r"c-[0-9a-f]{8}")
REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


class LearnError(Exception):
    """An error the user can act on; exits 1."""


class UsageError(Exception):
    """Bad arguments that argparse cannot catch; exits 64."""


# ----------------------------------------------------------------------------- small helpers

def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id():
    return secrets.token_hex(6)


def warn(msg):
    print(f"learn.py: warning: {msg}", file=sys.stderr)


def _s(value):
    """A record field as text: strings as they are, numbers as digits, anything else empty."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def truncate(text, limit=SUMMARY_MAX):
    """One line, at most `limit` characters."""
    s = " ".join(_s(text).split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def normalize_part(part):
    return " ".join(_s(part).lower().split())


def candidate_id(key):
    return "c-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def resalt(salt, project_id):
    if not project_id:
        return None
    return hashlib.sha256(("export:" + salt + project_id).encode("utf-8")).hexdigest()[:12]


def most_common(values):
    """Most frequent non-empty value; ties go to the one seen first."""
    values = [v for v in values if isinstance(v, str) and v]
    if not values:
        return ""
    counts = Counter(values)
    best = max(counts.values())
    return next(v for v in values if counts[v] == best)


# ----------------------------------------------------------------------------- redaction

_RX_URL = re.compile(r"\b(?:https?|ftp|wss?|ssh|git)://(?:[^\s/@'\"<>]*@)?([^\s/:?#'\"<>()\[\]{}]+)[^\s'\"<>()\[\]{}]*",
                     re.I)
_RX_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_RX_QUOTED_PATH = re.compile(r"""(["'])((?:[A-Za-z]:[\\/]|\\\\[^\\/\s"']|~?/[^\s/"']+/)[^"'\r\n]*)\1""")
_RX_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?]*")
_RX_UNC_PATH = re.compile(r"(?<![\w\\])\\\\[A-Za-z0-9._$-]+\\[^\s\"'<>|*?]*")
_RX_UNIX_PATH = re.compile(r"(?<![\w.:/\\~<>-])~?(?:/[^\s/\"'<>|()\[\]{},;`]+){2,}/?")
_RX_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")
_TRAILING = ".,;:!?"


def _url_sub(m):
    text, trail = m.group(0), ""
    while text and text[-1] in _TRAILING:
        trail, text = text[-1] + trail, text[:-1]
    host = m.group(1).rstrip(_TRAILING).lower()
    return f"<url:{host}>{trail}"


def redact(text):
    """Remove what could identify a person or a machine.

    Absolute paths (Windows, UNC and Unix) become <path>, emails <email>, URLs keep only the host
    (<url:github.com>), and long token-like strings (24+ of [A-Za-z0-9_-] with a digit) <token>.
    """
    s = _s(text)
    s = _RX_URL.sub(_url_sub, s)
    s = _RX_EMAIL.sub("<email>", s)
    s = _RX_QUOTED_PATH.sub(lambda m: m.group(1) + "<path>" + m.group(1), s)
    s = _RX_WIN_PATH.sub("<path>", s)
    s = _RX_UNC_PATH.sub("<path>", s)
    s = _RX_UNIX_PATH.sub("<path>", s)
    s = _RX_TOKEN.sub("<token>", s)
    return s


# ----------------------------------------------------------------------------- the store

def store_dir():
    env = os.environ.get("APP_BLUEPRINT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".app-blueprint"


def read_jsonl(path):
    """Records in a JSON-lines file. Skips blank and malformed lines; keeps the first of duplicate ids."""
    out, seen = [], set()
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return out
    except OSError as e:
        raise LearnError(f"cannot read {path}: {e}")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(rec)
    return out


def append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if needs_newline:
            f.write("\n")
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def get_salt():
    """The store's salt (32 hex chars). Created on first use, never overwritten once set."""
    path = store_dir() / SALT
    try:
        current = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        current = None
    if current:
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(16)
    if current is None:
        try:
            with open(path, "x", encoding="utf-8", newline="") as f:
                f.write(salt)
            return salt
        except FileExistsError:  # another writer got there first
            again = path.read_text(encoding="utf-8").strip()
            if again:
                return again
    path.write_text(salt, encoding="utf-8")  # the file existed but was empty
    return salt


def load_private_terms():
    """Terms from the store's private-terms.txt: one per line, `#` starts a comment."""
    path = store_dir() / PRIVATE_TERMS
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    terms, seen = [], set()
    for line in text.splitlines():
        term = line.split("#", 1)[0].strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def private_terms_regex(terms):
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)", re.I)


# ----------------------------------------------------------------------------- observations

def frame_function(frame):
    """'blueprint.py:142:load_plan' -> 'load_plan'."""
    return _s(frame).rsplit(":", 1)[-1].strip()


def incident_observation(rec):
    frames = [f for f in rec.get("frames") or [] if isinstance(f, str)] if isinstance(rec.get("frames"), list) else []
    error = _s(rec.get("error")).strip() or "Error"
    command = _s(rec.get("command")).strip() or "unknown"
    func = frame_function(frames[-1]) if frames else ""  # SPEC: frames are innermost last
    summary = f"{error} in {func} during {command}" if func else f"{error} during {command}"
    return {"id": "inc-" + rec["id"], "source": "incident", "kind": "bug", "part": f"command:{command}",
            "key": f"incident:{error}@{func or '?'}", "summary": truncate(summary),
            "projectId": _s(rec.get("projectId")) or None, "at": _s(rec.get("at")),
            "evidence": {"frames": frames, "message": redact(rec.get("message"))}}


def feedback_observation(rec):
    kind = _s(rec.get("kind")).strip().lower() or "bug"
    part = _s(rec.get("part")).strip()
    norm = normalize_part(part)
    # A note without a part names no part of the skill, so it stands alone instead of grouping
    # with every other part-less note of the same kind.
    key = f"{kind}:{norm}" if norm else f"{kind}:fb-{rec['id']}"
    note = _s(rec.get("note"))
    evidence = {"note": note}
    if _s(rec.get("by")):
        evidence["by"] = _s(rec.get("by"))
    return {"id": "fb-" + rec["id"], "source": "feedback", "kind": kind, "part": part, "key": key,
            "summary": truncate(redact(note)) or f"{kind} feedback", "projectId": _s(rec.get("projectId")) or None,
            "at": _s(rec.get("at")), "evidence": evidence}


def friction_observations(rec):
    codes = rec.get("codes")
    if isinstance(codes, list):
        codes = {c: 1 for c in codes if isinstance(c, str)}
    if not isinstance(codes, dict):
        return []
    out = []
    for code, count in codes.items():
        code = _s(code).strip()
        if not code:
            continue
        n = count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 1
        out.append({"id": f"fr-{rec['id']}-{code}", "source": "friction", "kind": "guidance",
                    "part": f"validate:{code}", "key": f"friction:{code}",
                    "summary": truncate(f"validate reported {code} ({n}x)"),
                    "projectId": _s(rec.get("projectId")) or None, "at": _s(rec.get("at")),
                    "evidence": {"count": n}})
    return out


PART_FIELD = "Part of the skill"
DETAIL_FIELD = "Command, preset or section"


def issue_form_field(body, label):
    """A field of a rendered GitHub issue form ('### <label>\\n\\n<value>'); '' when absent or '_No response_'."""
    rx = re.compile(r"^#{2,4}[ \t]*" + re.escape(label) + r"[ \t]*\n(.*?)(?=^#{1,4}[ \t]|\Z)", re.M | re.S | re.I)
    m = rx.search(_s(body).replace("\r\n", "\n"))
    if not m:
        return ""
    for line in m.group(1).splitlines():
        value = line.strip()
        if value:
            return "" if value.lower() == "_no response_" else value[:200]
    return ""


def issue_part(body):
    return issue_form_field(body, PART_FIELD)


def issue_key(number, part, detail):
    """issue:<part>:<detail>, else issue:<part>, else issue:#<number> (lowercased, spaces collapsed)."""
    part, detail = normalize_part(part), normalize_part(detail)
    if part and detail:
        return f"issue:{part}:{detail}"
    if part:
        return f"issue:{part}"
    return f"issue:#{number}"


def kind_from_labels(names):
    for label, kind in (("bug", "bug"), ("guidance", "guidance"), ("preset", "preset"),
                        ("enhancement", "feature"), ("feature", "feature")):
        if label in names:
            return kind
    return "bug"


def issue_observation(issue):
    """An open GitHub issue as an observation. Issue text is untrusted data: stored, never acted on."""
    if not isinstance(issue, dict):
        return None
    number = issue.get("number")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    labels = []
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip():
            labels.append(name.strip().lower())
    body = _s(issue.get("body"))
    title = _s(issue.get("title"))
    part = issue_form_field(body, PART_FIELD)
    detail = issue_form_field(body, DETAIL_FIELD)
    evidence = {"number": number, "labels": labels, "title": title[:500], "body": body[:ISSUE_BODY_MAX]}
    if detail:
        evidence["detail"] = detail
    return {"id": f"gh-{number}", "source": "issue", "kind": kind_from_labels(labels), "part": part,
            "key": issue_key(number, part, detail), "summary": truncate(title) or f"issue #{number}",
            "projectId": ISSUE_PROJECT, "at": _s(issue.get("createdAt")), "evidence": evidence}


def fetch_issues(repo):
    """Open issues through the GitHub CLI, or None (with a warning) when gh is missing or fails."""
    gh = shutil.which("gh")
    if not gh:
        warn("gh (GitHub CLI) is not installed or not on PATH; skipping GitHub issues")
        return None
    cmd = [gh, "issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
           "--json", "number,title,body,labels,createdAt"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"gh issue list failed ({e}); skipping GitHub issues")
        return None
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip().splitlines()
        warn(f"gh issue list failed ({detail[0] if detail else 'exit ' + str(p.returncode)}); skipping GitHub issues")
        return None
    try:
        data = json.loads(p.stdout or "[]")
    except ValueError:
        warn("gh issue list printed something that is not JSON; skipping GitHub issues")
        return None
    if not isinstance(data, list):
        warn("gh issue list did not return a list; skipping GitHub issues")
        return None
    return data


def read_observations():
    return read_jsonl(store_dir() / OBSERVATIONS)


# ----------------------------------------------------------------------------- candidates

def community_observation(member, exported):
    """A member of a shared bundle as a `community` observation. Bundles are untrusted input."""
    if not isinstance(member, dict):
        return None
    key = _s(member.get("key")).strip()[:300]
    if not key:
        return None
    rid = _s(member.get("id")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:#-]{1,80}", rid):
        rid = hashlib.sha1(json.dumps(member, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    evidence = member.get("evidence") if isinstance(member.get("evidence"), dict) else {}
    frames = evidence.get("frames") if isinstance(evidence.get("frames"), list) else []
    frames = [truncate(f, 200) for f in frames if isinstance(f, str)][:50]
    obs = {"id": "cm-" + rid, "source": "community", "origin": truncate(member.get("source"), 40),
           "kind": truncate(member.get("kind"), 40) or "bug", "part": truncate(member.get("part")),
           "key": key, "summary": truncate(member.get("summary")),
           "projectId": truncate(member.get("projectId"), 64) or None, "at": exported}
    if frames:
        obs["evidence"] = {"frames": frames}
    return obs


def load_community(folder):
    """Observations from every *.json bundle (learn.py export output) in a folder."""
    d = Path(folder)
    if not d.is_dir():
        raise LearnError(f"--include-community: {folder} is not a folder")
    out, seen = [], set()
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            warn(f"skipping {f.name}: not readable JSON")
            continue
        if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
            warn(f"skipping {f.name}: not an {BUNDLE_FORMAT} bundle")
            continue
        exported = truncate(data.get("exported"), 40)
        candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
        for cand in candidates:
            members = cand.get("members") if isinstance(cand, dict) and isinstance(cand.get("members"), list) else []
            for member in members:
                obs = community_observation(member, exported)
                if obs and obs["id"] not in seen:
                    seen.add(obs["id"])
                    out.append(obs)
    return out


def load_decisions():
    """The latest decision record per candidate id (by `at`, then file order)."""
    latest = {}
    for i, rec in enumerate(read_jsonl(store_dir() / DECISIONS_FILE)):
        cid = rec.get("candidate")
        if not isinstance(cid, str) or rec.get("decision") not in DECISIONS:
            continue
        rank = (_s(rec.get("at")), i)
        if cid not in latest or rank >= latest[cid][0]:
            latest[cid] = (rank, rec)
    return {cid: rec for cid, (_, rec) in latest.items()}


def _is_bug_issue(obs):
    ev = obs.get("evidence") if isinstance(obs.get("evidence"), dict) else {}
    labels = ev.get("labels") if isinstance(ev.get("labels"), list) else []
    return "bug" in [_s(x).lower() for x in labels]


def build_candidates(observations, min_projects=DEFAULT_MIN_PROJECTS, decisions=None):
    """Group observations by key and apply the promotion rule from SPEC.md.

    Project count: distinct projectIds of local members (incident, feedback, friction), plus 1 if
    the group has a GitHub issue, plus distinct projectIds of community members (bundles from other
    maintainers; issue-born community members are not counted twice). The count rule needs at least
    one local member, so community records and issues only corroborate.
    """
    decisions = decisions or {}
    groups = {}
    for obs in observations:
        key = obs.get("key")
        if isinstance(key, str) and key and isinstance(obs.get("id"), str):
            groups.setdefault(key, []).append(obs)
    out = []
    for key, members in groups.items():
        local = [m for m in members if m.get("source") in LOCAL_SOURCES]
        issues = [m for m in members if m.get("source") == "issue"]
        community = [m for m in members if m.get("source") == "community"]
        local_projects = {m.get("projectId") for m in local if isinstance(m.get("projectId"), str) and m.get("projectId")}
        community_projects = {m.get("projectId") for m in community
                              if isinstance(m.get("projectId"), str) and m.get("projectId") and m.get("origin") != "issue"}
        projects = len(local_projects) + (1 if issues else 0) + len(community_projects)
        reasons = []
        if any(m.get("source") == "incident" for m in local):
            reasons.append("crash with a traceback")
        if any(_is_bug_issue(m) for m in issues):
            reasons.append("GitHub issue labelled bug")
        if local and projects >= min_projects:
            reasons.append(f"seen in {projects} projects")
        if reasons:
            reason = "; ".join(reasons)
        elif not local and not issues:
            reason = "community records only"
        elif not local:
            reason = "GitHub issue without the bug label and no local reports"
        else:
            reason = f"seen in {projects} project{'s' if projects != 1 else ''}; needs {min_projects}"
        primary = (local + issues) or members
        cid = candidate_id(key)
        decision = decisions.get(cid)
        out.append({"id": cid, "key": key, "kind": most_common([m.get("kind") for m in primary]),
                    "part": most_common([m.get("part") for m in primary]), "qualifies": bool(reasons),
                    "reason": reason, "projects": projects, "members": [m["id"] for m in members],
                    "decision": decision.get("decision") if decision else None})
    out.sort(key=lambda c: (not c["qualifies"], c["decision"] is not None, -c["projects"], -len(c["members"]), c["key"]))
    return out


# ----------------------------------------------------------------------------- harvest

def cmd_harvest(args):
    store = store_dir()
    obs_path = store / OBSERVATIONS
    known = {o["id"] for o in read_jsonl(obs_path)}
    new, counts = [], Counter()
    already = 0

    def add(obs):
        nonlocal already
        if obs is None:
            return
        if obs["id"] in known:
            already += 1
            return
        known.add(obs["id"])
        new.append(obs)
        counts[obs["source"]] += 1

    for rec in read_jsonl(store / INCIDENTS):
        add(incident_observation(rec))
    for rec in read_jsonl(store / FEEDBACK):
        add(feedback_observation(rec))
    for rec in read_jsonl(store / FRICTION):
        for obs in friction_observations(rec):
            add(obs)
    issues_note = ""
    if args.issues:
        if not REPO_RE.fullmatch(args.repo):
            raise UsageError(f"--repo must look like OWNER/NAME, got {args.repo!r}")
        issues = fetch_issues(args.repo)
        if issues is None:
            issues_note = " GitHub issues were not read."
        else:
            for issue in issues:
                add(issue_observation(issue))
    if new:
        append_jsonl(obs_path, new)
    by_source = ", ".join(f"{s} {counts[s]}" for s in ("incident", "feedback", "friction", "issue") if counts[s])
    print(f"harvest: {len(new)} new observation{'s' if len(new) != 1 else ''}"
          + (f" ({by_source})" if by_source else "") + f"; {already} already known.{issues_note}")
    print(f"store: {store}")
    if new:
        print("next: learn.py promote")
    return EXIT_OK


# ----------------------------------------------------------------------------- promote and decide

def _candidates_for(args):
    observations = read_observations()
    if getattr(args, "include_community", None):
        observations = observations + load_community(args.include_community)
    return observations, build_candidates(observations, args.min_projects, load_decisions())


def cmd_promote(args):
    observations, cands = _candidates_for(args)
    shown = cands if args.all else [c for c in cands if c["qualifies"] and c["decision"] is None]
    if args.json:
        print(json.dumps(shown, indent=2, ensure_ascii=False))
        return EXIT_OK
    by_id = {o["id"]: o for o in observations}
    decisions = load_decisions()
    qualifying = sum(1 for c in cands if c["qualifies"])
    decided = sum(1 for c in cands if c["decision"] is not None)
    print(f"{len(cands)} group{'s' if len(cands) != 1 else ''} from {len(observations)} observations: "
          f"{qualifying} qualif{'y' if qualifying != 1 else 'ies'}, {decided} decided (min projects {args.min_projects}).")
    if not shown:
        print("Nothing to decide." + ("" if args.all else " Use --all to list every group."))
        return EXIT_OK
    print("Showing " + ("every group." if args.all else "qualifying candidates without a decision.")
          + " Decide with: learn.py decide <id> adopt|reject|defer --note TEXT")
    for c in shown:
        print()
        print(f"{c['id']}  {c['kind'] or '-'}  {c['part'] or '-'}")
        print(f"  {'qualifies' if c['qualifies'] else 'does not qualify'}: {c['reason']} · "
              f"{c['projects']} project{'s' if c['projects'] != 1 else ''}")
        print(f"  key: {c['key']}")
        dec = decisions.get(c["id"])
        if dec:
            who = f" by {_s(dec.get('by'))}" if _s(dec.get("by")) else ""
            print(f"  decision: {dec.get('decision')}{who} at {_s(dec.get('at')) or '?'}: {truncate(dec.get('note'))}")
        print(f"  members ({len(c['members'])}):")
        for mid in c["members"]:
            o = by_id.get(mid, {})
            print(f"    {mid}  [{_s(o.get('source'))}]  {truncate(o.get('summary'), 160)}")
    return EXIT_OK


def cmd_decide(args):
    cid = args.candidate
    if not CANDIDATE_RE.fullmatch(cid):
        raise UsageError(f"candidate ids look like c-1a2b3c4d, got {cid!r}")
    if not args.force:
        ids = {c["id"] for c in build_candidates(read_observations(), DEFAULT_MIN_PROJECTS, {})}
        if cid not in ids:
            raise LearnError(f"no candidate {cid} in the current promote output "
                             "(list them with: learn.py promote --all; record anyway with --force)")
    rec = {"id": new_id(), "at": now_iso(), "candidate": cid, "decision": args.decision, "note": args.note,
           "by": args.by}
    append_jsonl(store_dir() / DECISIONS_FILE, [rec])
    print(f"recorded: {cid} {args.decision} by {args.by} ({rec['id']}) in {store_dir() / DECISIONS_FILE}")
    return EXIT_OK


# ----------------------------------------------------------------------------- scan-private

_SCAN_WIN_HOME = re.compile(r"(?<![A-Za-z0-9])(?P<prefix>[A-Za-z]:[\\/]+Users[\\/]+)(?P<name>[^\\/\s\"'`<>|:*?]+)", re.I)
_SCAN_UNIX_HOME = re.compile(r"(?<![\w.~:-])(?P<prefix>(?:(?:/mnt)?/[A-Za-z])?/(?:Users|home)/)(?P<name>[^/\s\"'`<>|:*?]+)")
_HOME_PLACEHOLDERS = {
    "you", "your", "yours", "yourname", "your-name", "your_name", "yourusername", "your-username",
    "your_username", "user", "username", "user-name", "user_name", "someone", "somebody", "name", "me",
    "example", "public", "default", "default user", "all users", "runner", "x", "xxx",
}
_SCAN_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_SCAN_ARTIFACT = re.compile(r"(?P<prefix>claude\.ai/artifacts?/)(?P<id>[A-Za-z0-9_-]*)", re.I)
_SCAN_TOKENS = [
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_])[rs]k_live_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}"),
    re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
]


def mask(text, keep=2, cap=None):
    """Keep the first `keep` characters, star the rest."""
    stars = max(len(text) - keep, 1)
    if cap:
        stars = min(stars, cap)
    return text[:keep] + "*" * stars


def _home_placeholder(name):
    n = name.strip().lower()
    return not n or n in _HOME_PLACEHOLDERS or n[0] in "<{[$%(" or not any(ch.isalnum() for ch in n)


def scan_line(line, terms_re=None):
    """(kind, masked match) for each piece of private data in one line."""
    hits = []
    for rx in (_SCAN_WIN_HOME, _SCAN_UNIX_HOME):
        for m in rx.finditer(line):
            if not _home_placeholder(m.group("name")):
                hits.append(("home-path", m.group("prefix") + mask(m.group("name"))))
    for m in _SCAN_EMAIL.finditer(line):
        email = m.group(0).lower()
        if not (email.endswith("@users.noreply.github.com") or email == "noreply@anthropic.com"):
            hits.append(("email", mask(m.group(0))))
    for m in _SCAN_ARTIFACT.finditer(line):
        hits.append(("artifact-link", m.group("prefix") + (mask(m.group("id")) if m.group("id") else "")))
    for rx in _SCAN_TOKENS:
        for m in rx.finditer(line):
            hits.append(("token", mask(m.group(0), keep=4, cap=16)))
    if terms_re:
        for m in terms_re.finditer(line):
            hits.append(("private-term", mask(m.group(0))))
    unique = []
    for h in hits:
        if h not in unique:
            unique.append(h)
    return unique


def scan_text(text, terms_re=None):
    """(line number, kind, masked match) for a whole text. Lines marked `scan-private: allow` are skipped."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        if ALLOW_MARK in line:
            continue
        out.extend((n, kind, masked) for kind, masked in scan_line(line, terms_re))
    return out


def _git_paths(args, cwd):
    """Repo-relative paths from git, resolved against the repo top level."""
    git = shutil.which("git")
    if not git:
        raise LearnError("git is not installed or not on PATH")

    def run(cmd):
        p = subprocess.run([git] + cmd, cwd=str(cwd), capture_output=True)
        if p.returncode != 0:
            raise LearnError(f"git {' '.join(cmd)} failed: "
                             + (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[0])
        return p.stdout.decode("utf-8", "replace")

    top = Path(run(["rev-parse", "--show-toplevel"]).strip())
    names = []
    if args.staged:
        names += [n for n in run(["diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"]).split("\0") if n]
    if args.all:
        names += [n for n in run(["ls-files", "-z", "--full-name"]).split("\0") if n]
    return [top / n for n in names]


def _display(path, cwd):
    try:
        rel = os.path.relpath(str(path), str(cwd))
    except ValueError:  # another drive on Windows
        return Path(path).as_posix()
    return Path(path).as_posix() if rel.startswith("..") else Path(rel).as_posix()


def collect_scan_targets(paths, cwd, git_paths=()):
    """Files to scan: given files, folders walked recursively (skipping .git and __pycache__), git paths."""
    files, missing, seen = [], [], set()

    def add(p):
        key = os.path.normcase(os.path.abspath(str(p)))
        if key not in seen:
            seen.add(key)
            files.append(Path(p))

    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(cwd) / p
        if p.is_dir():
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SCAN_SKIP_DIRS)
                for name in sorted(names):
                    add(Path(root) / name)
        elif p.is_file():
            add(p)
        else:
            missing.append(raw)
    for p in git_paths:
        if p.is_file():
            add(p)
    return files, missing


def scan_files(files, terms_re=None):
    """Yields (path, hits) for every scanned file; binary files and files over 2 MB are skipped."""
    for path in files:
        try:
            if path.stat().st_size > SCAN_MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        yield path, scan_text(data.decode("utf-8", errors="replace"), terms_re)


def cmd_scan_private(args):
    if not (args.paths or args.staged or args.all):
        raise UsageError("give files or folders to scan, or --staged, or --all")
    cwd = Path.cwd()
    git_paths = _git_paths(args, cwd) if (args.staged or args.all) else []
    files, missing = collect_scan_targets(args.paths, cwd, git_paths)
    for raw in missing:
        print(f"learn.py: error: {raw}: no such file or folder", file=sys.stderr)
    terms_re = private_terms_regex(load_private_terms())
    scanned = hit_files = total = 0
    for path, hits in scan_files(files, terms_re):
        scanned += 1
        if hits:
            hit_files += 1
        for line_no, kind, masked in hits:
            total += 1
            print(f"{_display(path, cwd)}:{line_no}: {kind}: {masked}")
    if total:
        print(f"scan-private: {total} finding{'s' if total != 1 else ''} in {hit_files} file{'s' if hit_files != 1 else ''} "
              f"({scanned} scanned). Remove them, or end a line that must stay with '# {ALLOW_MARK}'.", file=sys.stderr)
    else:
        print(f"scan-private: clean ({scanned} file{'s' if scanned != 1 else ''} scanned).", file=sys.stderr)
    return EXIT_FINDINGS if (total or missing) else EXIT_OK


# ----------------------------------------------------------------------------- export

def build_export(observations, decisions=None, salt="", terms=(), project_names=()):
    """The shareable bundle: qualifying candidates only, text redacted, projectIds re-salted.

    `terms` (private-terms.txt) become <private> and local project names <project> wherever they
    appear in the text that is kept.
    """
    terms_re = private_terms_regex(list(terms))
    names_re = private_terms_regex([n for n in project_names if len(n) >= 3])

    def clean(text):
        out = redact(text)
        if terms_re:
            out = terms_re.sub("<private>", out)
        if names_re:
            out = names_re.sub("<project>", out)
        return out

    by_id = {o["id"]: o for o in observations}
    candidates = []
    for c in build_candidates(observations, DEFAULT_MIN_PROJECTS, decisions or {}):
        if not c["qualifies"]:
            continue
        members = []
        for mid in c["members"]:
            o = by_id.get(mid)
            if not o or o.get("source") == "community":
                continue
            member = {"id": mid, "source": _s(o.get("source")), "kind": _s(o.get("kind")), "part": clean(o.get("part")),
                      "key": clean(o.get("key")), "summary": truncate(clean(o.get("summary"))),
                      "projectId": resalt(salt, _s(o.get("projectId")))}
            ev = o.get("evidence") if isinstance(o.get("evidence"), dict) else {}
            frames = [clean(f) for f in ev.get("frames") or [] if isinstance(f, str)] if isinstance(ev.get("frames"), list) else []
            if frames:
                member["evidence"] = {"frames": frames}
            members.append(member)
        candidates.append({"id": c["id"], "key": clean(c["key"]), "kind": c["kind"], "part": clean(c["part"]),
                           "qualifies": True, "reason": c["reason"], "projects": c["projects"], "members": members})
    return {"format": BUNDLE_FORMAT, "exported": now_iso(), "candidates": candidates}


def local_project_names():
    """Project folder names recorded in the store (they stay local; export scrubs them from text)."""
    names = []
    for name in (INCIDENTS, FEEDBACK, FRICTION):
        for rec in read_jsonl(store_dir() / name):
            project = _s(rec.get("project")).strip()
            if project and project not in names:
                names.append(project)
    return names


def cmd_export(args):
    bundle = build_export(read_observations(), load_decisions(), get_salt(), load_private_terms(),
                          local_project_names())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    n_members = sum(len(c["members"]) for c in bundle["candidates"])
    print(f"exported {len(bundle['candidates'])} candidate{'s' if len(bundle['candidates']) != 1 else ''} "
          f"({n_members} observations) to {out}")
    print(f"Read it before sharing, then check it: learn.py scan-private {out}")
    return EXIT_OK


# ----------------------------------------------------------------------------- store (git)

def _git(args, cwd):
    git = shutil.which("git")
    if not git:
        raise LearnError("git is not installed or not on PATH")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run([git] + args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _git_ok(args, cwd):
    p = _git(args, cwd)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        raise LearnError(f"git {' '.join(a for a in args if not a.startswith('user.'))} failed in {cwd}:\n{detail}")
    return p


def _git_identity(store):
    """`-c` flags so commits work on a machine with no git identity configured."""
    def configured(key):
        p = _git(["config", "--get", key], store)
        return p.returncode == 0 and p.stdout.strip()
    env = os.environ
    flags = []
    if not (configured("user.name") or (env.get("GIT_AUTHOR_NAME") and env.get("GIT_COMMITTER_NAME"))):
        flags += ["-c", "user.name=app-blueprint store"]
    if not (configured("user.email") or (env.get("GIT_AUTHOR_EMAIL") and env.get("GIT_COMMITTER_EMAIL"))):
        flags += ["-c", "user.email=app-blueprint@localhost"]
    return flags


def _git_branch(store):
    p = _git(["symbolic-ref", "--short", "HEAD"], store)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else "main"


def _mask_url(url):
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", url)


def dedupe_jsonl(path):
    """Drop repeated ids (keep the first) and blank lines. Unparseable lines are kept. Returns lines removed."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    kept, seen = [], set()
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            rec = json.loads(text)
        except ValueError:
            rec = None
        rid = rec.get("id") if isinstance(rec, dict) else None
        if isinstance(rid, str) and rid:
            if rid in seen:
                continue
            seen.add(rid)
        kept.append(line.rstrip("\r"))
    if kept == lines:
        return 0
    removed = len(lines) - len(kept)
    path.write_text("".join(x + "\n" for x in kept), encoding="utf-8", newline="\n")
    return removed


def _resolve_pull_conflicts(store, conflicted):
    """Resolve what is safe to resolve: the salt (the remote's wins, so every machine converges)
    and private-terms.txt (the union of both sides). Returns False if anything else conflicts."""
    if not set(conflicted) <= {SALT, PRIVATE_TERMS}:
        return False
    if SALT in conflicted:
        _git_ok(["checkout", "--theirs", "--", SALT], store)
    if PRIVATE_TERMS in conflicted:
        merged = []
        for stage in (":2:", ":3:"):
            p = _git(["show", stage + PRIVATE_TERMS], store)
            for line in (p.stdout if p.returncode == 0 else "").splitlines():
                if line not in merged:
                    merged.append(line)
        (store / PRIVATE_TERMS).write_text("".join(x + "\n" for x in merged), encoding="utf-8", newline="\n")
    _git_ok(["add", "--"] + list(conflicted), store)
    return True


def cmd_store_init(args):
    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    done = []
    if not (store / ".git").exists():
        _git_ok(["init", "-q"], store)
        _git_ok(["symbolic-ref", "HEAD", "refs/heads/main"], store)
        done.append("created a git repository (branch main)")
    attrs = store / ".gitattributes"
    line = "*.jsonl merge=union"
    current = attrs.read_text(encoding="utf-8") if attrs.exists() else ""
    if line not in current.splitlines():
        attrs.write_text(current + ("" if not current or current.endswith("\n") else "\n") + line + "\n",
                         encoding="utf-8", newline="\n")
        done.append("wrote .gitattributes (*.jsonl merge=union)")
    ignore = store / ".gitignore"
    if not ignore.exists():
        # private-terms.txt is deliberately not ignored: it syncs to the owner's private remote.
        ignore.write_text("*.tmp\n*.bak\n.DS_Store\nThumbs.db\n", encoding="utf-8", newline="\n")
        done.append("wrote .gitignore")
    if args.remote:
        existing = _git(["remote", "get-url", "origin"], store)
        if existing.returncode != 0:
            _git_ok(["remote", "add", "origin", args.remote], store)
            done.append(f"added remote origin {_mask_url(args.remote)}")
        elif existing.stdout.strip() != args.remote:
            _git_ok(["remote", "set-url", "origin", args.remote], store)
            done.append(f"changed remote origin to {_mask_url(args.remote)}")
    print(f"store: {store}")
    for d in done or ["already set up; nothing to change"]:
        print(f"  {d}")
    print("The remote must be private: the store holds project names and your notes.")
    print("next: learn.py store sync")
    return EXIT_OK


def cmd_store_sync(args):
    store = store_dir()
    if not (store / ".git").exists():
        raise LearnError(f"{store} is not a git repository yet; run: learn.py store init [--remote URL]")
    ident = _git_identity(store)
    branch = _git_branch(store)
    stamp = now_iso()
    report = []
    _git_ok(["add", "-A"], store)
    if _git(["status", "--porcelain"], store).stdout.strip():
        _git_ok(ident + ["commit", "-q", "-m", f"store sync {stamp}"], store)
        report.append("committed local changes")
    else:
        report.append("no local changes to commit")
    if _git(["remote", "get-url", "origin"], store).returncode != 0:
        report.append("no remote 'origin': nothing pulled or pushed (add one: learn.py store init --remote URL)")
    else:
        heads = _git(["ls-remote", "--heads", "origin", branch], store)
        if heads.returncode != 0:
            detail = (heads.stderr or "").strip().splitlines()
            raise LearnError(f"cannot reach origin: {detail[0] if detail else 'git ls-remote failed'}")
        if heads.stdout.strip():
            pull = _git(ident + ["pull", "--no-rebase", "--no-edit", "--allow-unrelated-histories", "origin", branch], store)
            if pull.returncode != 0:
                conflicted = [n for n in _git(["diff", "--name-only", "--diff-filter=U"], store).stdout.splitlines() if n]
                if conflicted and _resolve_pull_conflicts(store, conflicted):
                    _git_ok(ident + ["commit", "-q", "--no-edit"], store)
                    report.append("resolved merge conflicts in " + ", ".join(conflicted)
                                  + (" (kept the remote's salt)" if SALT in conflicted else ""))
                else:
                    detail = (pull.stderr or pull.stdout or "").strip()
                    raise LearnError(f"git pull failed in {store}:\n{detail}\n"
                                     "Resolve it there (git status), commit, then run store sync again.")
            report.append(f"pulled origin/{branch}")
            removed = sum(dedupe_jsonl(p) for p in sorted(store.glob("*.jsonl")))
            if removed:
                _git_ok(["add", "-A"], store)
                if _git(["status", "--porcelain"], store).stdout.strip():
                    _git_ok(ident + ["commit", "-q", "-m", f"store sync {stamp}: de-duplicate records"], store)
                report.append(f"removed {removed} duplicate line{'s' if removed != 1 else ''} after the merge")
        else:
            report.append(f"origin has no {branch} branch yet")
        _git_ok(["push", "-q", "origin", branch], store)
        report.append(f"pushed to origin/{branch}")
    print(f"store sync: {store}")
    for r in report:
        print(f"  {r}")
    return EXIT_OK


def cmd_store_status(args):
    store = store_dir()
    print(f"store: {store}" + ("" if store.is_dir() else "  (not created yet)"))
    if store.is_dir():
        print(f"  salt: {'present' if (store / SALT).exists() else 'not created yet'}")
        terms = load_private_terms()
        print(f"  private terms: {len(terms)}" + ("" if (store / PRIVATE_TERMS).exists() else " (no private-terms.txt)"))
        for name in STORE_FILES:
            print(f"  {name:<20} {len(read_jsonl(store / name))}")
    if not (store / ".git").exists():
        print("  git: not a repository (learn.py store init [--remote URL] to sync it between machines)")
        return EXIT_OK
    remote = _git(["remote", "get-url", "origin"], store)
    print(f"  git: branch {_git_branch(store)}, remote "
          + (f"origin {_mask_url(remote.stdout.strip())}" if remote.returncode == 0 else "none"))
    last = _git(["log", "-1", "--format=%h %cI %s"], store)
    print(f"  last commit: {last.stdout.strip() if last.returncode == 0 and last.stdout.strip() else 'none'}")
    dirty = [line for line in _git(["status", "--porcelain"], store).stdout.splitlines() if line.strip()]
    print(f"  uncommitted changes: {len(dirty)}")
    return EXIT_OK


# ----------------------------------------------------------------------------- presets

_RX_DOC_URL = re.compile(r"https?://[^\s,;()<>\[\]\"'`|]+")


def _cells(line):
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def parse_stack_table(text):
    """Rows of the '## Stack' table: Part | Choice | Version checked (or seen) | Docs to re-check."""
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^##\s+Stack\b", l)), None)
    if start is None:
        return []
    header, rows = None, []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if not stripped.startswith("|"):
            if header is not None:
                break
            continue
        cells = _cells(stripped)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue

        def col(prefix, fallback):
            idx = next((i for i, h in enumerate(header) if h.startswith(prefix)), fallback)
            return cells[idx] if idx < len(cells) else ""

        docs = col("docs", 3)
        rows.append({"part": col("part", 0), "choice": col("choice", 1), "version": col("version", 2),
                     "docs": [u.rstrip(".") for u in _RX_DOC_URL.findall(docs)]})
    return rows


def load_presets(root=REPO_ROOT):
    """{preset id: rows} for app-blueprint/references/stacks/*.md under `root` (not the local/ folder)."""
    stacks = Path(root) / STACKS_REL
    out = {}
    for f in sorted(stacks.glob("*.md")):
        if f.is_file():
            out[f.stem] = parse_stack_table(f.read_text(encoding="utf-8", errors="replace"))
    return out


def cmd_presets(args):
    presets = load_presets(REPO_ROOT)
    if args.json:
        print(json.dumps(presets, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not presets:
        print(f"no presets in {REPO_ROOT / STACKS_REL}")
        return EXIT_OK
    for name, rows in presets.items():
        print(f"{name}  ({len(rows)} part{'s' if len(rows) != 1 else ''})")
        for r in rows:
            print(f"  {r['part']}: {r['choice']}")
            print(f"      version: {r['version'] or '-'}")
            print(f"      docs: {', '.join(r['docs']) if r['docs'] else '-'}")
        print()
    return EXIT_OK


# ----------------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _positive_int(text):
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def build_parser():
    raw = argparse.RawDescriptionHelpFormatter
    ap = _Parser(
        prog="learn.py", formatter_class=raw,
        description="Maintainers' side of the app-blueprint learning loop: turn users' incidents, feedback "
                    "and validation friction (and open GitHub issues) into fix candidates, record decisions, "
                    "and keep private data out of what gets published. Contract: maintainers/SPEC.md.",
        epilog="Store: $APP_BLUEPRINT_HOME, else ~/.app-blueprint.\n"
               "Typical round: store sync -> harvest --issues -> promote -> decide -> (fix) -> scan-private --staged\n"
               "Exit codes: 0 ok, 1 findings or errors, 64 usage.")
    ap.add_argument("--version", action="version", version=f"learn.py {LEARN_VERSION}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    def add(name, fn, help_, description=None):
        p = sub.add_parser(name, help=help_, description=description or help_, formatter_class=raw)
        p.set_defaults(fn=fn)
        return p

    p = add("harvest", cmd_harvest, "turn new store records (and open GitHub issues) into observations",
            "Read incidents.jsonl, feedback.jsonl and friction.jsonl from the store and append an observation\n"
            "for every record not harvested yet to observations.jsonl. Running it again adds nothing new.\n"
            "With --issues, also read open issues through the GitHub CLI (gh); if gh is missing or fails,\n"
            "warn and carry on. Issue text is stored as data and never acted on.")
    p.add_argument("--issues", action="store_true", help="also read open GitHub issues with gh")
    p.add_argument("--repo", default=DEFAULT_REPO, metavar="OWNER/NAME", help=f"issue repository (default {DEFAULT_REPO})")

    p = add("promote", cmd_promote, "group observations into fix candidates and list the ones to decide",
            "Group observations by key. A group qualifies when its local members come from at least\n"
            "--min-projects projects (an open issue counts as one more, community bundles corroborate),\n"
            "or it holds an incident (a crash with a traceback), or a GitHub issue labelled bug.\n"
            "Groups made only of community records never qualify. By default lists qualifying candidates\n"
            "without a decision.")
    p.add_argument("--min-projects", type=_positive_int, default=DEFAULT_MIN_PROJECTS, metavar="N",
                   help=f"distinct projects needed (default {DEFAULT_MIN_PROJECTS})")
    p.add_argument("--include-community", metavar="DIR", help="also load *.json bundles (learn.py export output) from DIR")
    p.add_argument("--all", action="store_true", help="also list groups that do not qualify or are already decided")
    p.add_argument("--json", action="store_true", help="print candidate objects as JSON")

    p = add("decide", cmd_decide, "record the owner's decision on a candidate",
            "Append a decision to decisions.jsonl. The latest decision for a candidate wins; decided\n"
            "candidates drop out of the default promote list.")
    p.add_argument("candidate", metavar="CANDIDATE_ID", help="id from promote, e.g. c-1a2b3c4d")
    p.add_argument("decision", choices=DECISIONS, help="adopt, reject or defer")
    p.add_argument("--note", required=True, help="why, and what happens next (e.g. 'fix in 1.2.1')")
    p.add_argument("--by", default="owner", help="who decided (default: owner)")
    p.add_argument("--force", action="store_true", help="record it even if the id is not in the current promote output")

    p = add("scan-private", cmd_scan_private, "fail if files hold private data (exit 1 on any finding)",
            "Look for home-folder paths with a real user name, personal emails (GitHub noreply addresses and\n"
            "noreply@anthropic.com are fine), private claude.ai artifact links, token-like strings, and\n"
            "terms from the store's private-terms.txt (case-insensitive, whole words). Lines containing\n"
            "'scan-private: allow' are skipped. Folders are walked recursively, skipping .git and\n"
            "__pycache__; binary files and files over 2 MB are skipped.\n"
            "Prints path:line: kind: masked-match.")
    p.add_argument("paths", nargs="*", metavar="PATH", help="files or folders to scan")
    p.add_argument("--staged", action="store_true", help="scan files staged for commit (added, copied, modified)")
    p.add_argument("--all", action="store_true", help="scan every file git tracks")

    p = add("export", cmd_export, "write a sanitized bundle of qualifying candidates to share by pull request",
            "Write qualifying candidates as an app-blueprint-lessons/1 bundle. Members keep only source, kind,\n"
            "part, key, redacted summary, incident frames and a re-salted projectId; project names, notes\n"
            "and decisions stay in the store. Terms from private-terms.txt become <private>.")
    p.add_argument("--out", required=True, metavar="FILE", help="where to write the bundle (JSON)")

    p = add("store", None, "set up, sync or inspect the store's git repository")
    ssub = p.add_subparsers(dest="store_cmd", metavar="ACTION", required=True)
    s = ssub.add_parser("init", help="make the store a git repository",
                        description="git init the store (branch main), write .gitattributes (*.jsonl merge=union) and\n"
                                    "a .gitignore, and add the remote 'origin' if given. The remote must be private.",
                        formatter_class=raw)
    s.add_argument("--remote", metavar="URL", help="private git remote shared by your machines")
    s.set_defaults(fn=cmd_store_init)
    s = ssub.add_parser("sync", help="commit, pull and push the store",
                        description="Commit local changes, pull origin (merge, no rebase), drop duplicate record ids\n"
                                    "that the merge produced, and push.",
                        formatter_class=raw)
    s.set_defaults(fn=cmd_store_sync)
    s = ssub.add_parser("status", help="where the store is, record counts and git state",
                        description="Print the store path, record counts per file, the git remote and the last commit.",
                        formatter_class=raw)
    s.set_defaults(fn=cmd_store_status)

    p = add("presets", cmd_presets, "list every stack part, version and docs URL in the published presets",
            "Parse the '## Stack' table of app-blueprint/references/stacks/*.md (not the local/ folder).")
    p.add_argument("--json", action="store_true", help="print {preset: [{part, choice, version, docs}]}")
    return ap


def _utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv=None):
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or EXIT_OK
    except UsageError as e:
        print(f"learn.py: error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except LearnError as e:
        print(f"learn.py: error: {e}", file=sys.stderr)
        return EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
