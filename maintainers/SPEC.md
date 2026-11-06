# Learning loop: shared formats (v1)

This file is the contract between `app-blueprint/scripts/blueprint.py` (which users run), `maintainers/learn.py` (which maintainers run) and the maintainer agents. Change it only together with all three.

## The store

Location: `$APP_BLUEPRINT_HOME`, or `~/.app-blueprint` when that is unset. It lives outside every project, on one machine, and is never published. `learn.py store init --remote <private-git-url>` makes it a git repository so several of the owner's machines can share it.

| File | Written by | One JSON object per line |
|---|---|---|
| `salt` | `learn.py`, on first use (plain text, 32 hex chars); used only to re-fingerprint projects in exports | — |
| `incidents.jsonl` | blueprint.py, on an unexpected error | incident |
| `feedback.jsonl` | `blueprint.py feedback` | feedback |
| `friction.jsonl` | `blueprint.py validate`, when it finds errors | friction |
| `observations.jsonl` | `learn.py harvest` | observation |
| `decisions.jsonl` | `learn.py decide` | decision |
| `private-terms.txt` | the owner, by hand (optional) | one private term per line; `#` starts a comment |
| `.gitattributes` | `learn.py store init` | `*.jsonl merge=union` |

Readers must tolerate blank or malformed lines (skip them) and duplicate ids (keep the first).

### Common fields

Every record has `id` (12 hex chars, random) and `at` (UTC ISO 8601, `2026-11-06T12:00:00Z`). Records written by blueprint.py (incident, feedback, friction) also have `tool`, the blueprint.py version. Records about a project also have:

- `projectId`: the first 12 hex chars of `sha256("app-blueprint:" + identity)`, where identity is the project's git `origin` URL (scheme, credentials and `.git` removed, lowercased) plus `#` and the project's folder inside the repo, or the absolute project path when there is no remote. The same project on two machines gets the same id, so it counts once. It counts projects without naming them.
- `project`: the project folder's name. Local only; `learn.py export` drops it.

### incident

```json
{"id": "3f2a9c1b7d4e", "at": "...", "tool": "1.2.0", "python": "3.12.4", "os": "Windows",
 "command": "sync", "error": "TypeError", "message": "string indices must be integers",
 "frames": ["blueprint.py:1301:main", "blueprint.py:142:load_plan"],
 "projectId": "a1b2c3d4e5f6", "project": "my-app"}
```

`message` is redacted: absolute paths become `<path>`, emails `<email>`, URLs keep only the host (`<url:github.com>`), long tokens `<token>`. `frames` hold only file basename, line and function, innermost last.

### feedback

```json
{"id": "...", "at": "...", "tool": "1.2.0", "kind": "guidance", "part": "SKILL.md step 4",
 "note": "Drafted P1 without a test file; the walking skeleton needs one", "by": "claude-code",
 "projectId": "...", "project": "my-app"}
```

`kind` is one of `bug`, `guidance`, `preset`, `feature`. `part` is free text naming the part of the skill: `SKILL.md step 4`, `preset:nextjs-prisma`, `command:sync`, `map:mind map`.

### friction

```json
{"id": "...", "at": "...", "tool": "1.2.0", "codes": {"owner-overlap": 2, "missing-verify": 1},
 "projectId": "...", "project": "my-app"}
```

Codes come from blueprint.py's validation rules (see `VALIDATION_CODES` in blueprint.py).

### observation (learn.py harvest)

```json
{"id": "inc-3f2a9c1b7d4e", "source": "incident", "kind": "bug", "part": "command:sync",
 "key": "incident:TypeError@load_plan", "summary": "TypeError in load_plan during sync",
 "projectId": "a1b2c3d4e5f6", "at": "...", "evidence": {"frames": ["..."], "message": "..."}}
```

- `source`: `incident`, `feedback`, `friction`, `issue` or `community`.
- `id`: `inc-<incident id>`, `fb-<feedback id>`, `fr-<friction id>-<code>`, `gh-<issue number>`, `cm-<bundle record id>`.
- `key` groups observations that describe the same problem:
  - incident: `incident:<error>@<innermost frame function>`;
  - feedback: `<kind>:<part lowercased, spaces collapsed>`; feedback with an empty part stands alone as `<kind>:fb-<id>`;
  - friction: `friction:<code>`;
  - issue: `issue:<part>:<detail>` from the issue form's "Part of the skill" and "Command, preset or section" fields (lowercased, spaces collapsed); `issue:<part>` when only the first is set; else `issue:#<number>`.

### candidate (learn.py promote output)

```json
{"id": "c-1a2b3c4d", "key": "...", "kind": "bug", "part": "command:sync", "qualifies": true,
 "reason": "crash with a traceback", "projects": 1, "members": ["inc-...", "gh-12"], "decision": null}
```

`id` is `c-` plus the first 8 hex chars of `sha1(key)`. A group qualifies when:
- it has at least one local member, and its distinct `projectId`s (local, plus community ones, plus one if a GitHub issue is in the group) reach `--min-projects` (default 2); or
- it contains an incident (a crash with a traceback); or
- it contains a GitHub issue labelled `bug`.

Community records only corroborate: a group made only of community records never qualifies. Issue keys (`issue:...`) never match local keys, so an issue qualifies through the `bug` label or by the owner's judgment; the harvester points out issues that describe the same problem as a local group.

### decision (learn.py decide)

```json
{"id": "...", "at": "...", "candidate": "c-1a2b3c4d", "decision": "adopt", "note": "fix in 1.2.1", "by": "owner"}
```

`decision` is `adopt`, `reject` or `defer`. The latest decision for a candidate wins.

## blueprint.py commands that feed the loop

| Command | Effect |
|---|---|
| any command that hits an unexpected error | writes an incident, prints its id, exits 70 |
| `feedback --kind K --note TEXT [--part P] [--by ID]` | writes feedback to the store and a `feedback` event to the project's `log.jsonl` |
| `report [INCIDENT_ID]` | prints a redacted GitHub issue body for that incident, or the latest; never files it |
| `doctor` | tool version, copied tool vs installed skill, store location |
| `validate` (with errors) | writes friction |

Set `APP_BLUEPRINT_NO_CAPTURE=1` to turn off all writes to the store.

## learn.py commands (maintainers only; standard library, Python 3.10+)

| Command | Effect |
|---|---|
| `harvest [--issues] [--repo OWNER/NAME]` | turns new store records, and with `--issues` open GitHub issues read through `gh`, into observations |
| `promote [--min-projects N] [--include-community DIR] [--all] [--json]` | groups observations and lists candidates; `--all` also lists those that do not qualify or are decided |
| `decide <candidate-id> adopt\|reject\|defer --note TEXT [--by ID]` | records the owner's decision |
| `scan-private [PATHS...] [--staged] [--all]` | exits 1 if files contain home-folder paths, personal emails (not GitHub noreply), private claude.ai artifact links, token-like strings, or any term from `private-terms.txt`. Lines containing `scan-private: allow` are skipped |
| `export --out FILE` | a sanitized bundle of qualifying candidates for sharing by pull request: no project names, fingerprints re-salted, text redacted |
| `store init [--remote URL]` · `store sync` · `store status` | make the store a git repo; commit, pull and push it; show where it is |
| `presets [--json]` | every stack part, version and docs URL in the published presets, for the preset scout |
