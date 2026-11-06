# Maintainers

How the app-blueprint skill learns from use, and how changes get from a problem to a release. Users of the skill do not need this folder.

The loop follows two existing designs. From [adminwright](https://github.com/TarikAI/adminwright): harvest signals into a store outside every project, and promote a lesson only when two or more projects show it. From Google's [Elephant-Goldfish Model](https://github.com/google/elephant-goldfish-model): every change is reviewed by an agent with no context, which judges only the evidence, the diff and the tests.

| Path | What it is |
|---|---|
| [SPEC.md](SPEC.md) | the contract: store files, record formats, `learn.py` and the capture commands in blueprint.py |
| `learn.py` | the maintainer CLI: harvest, promote, decide, scan-private, export, store, presets |
| `tests/` | tests for `learn.py` |
| `make_example.py` | regenerates `examples/snag-desk` with the current tool |
| [agents/](agents/) | four subagents: harvester, fixer, reviewer, preset scout |
| `triage/` | reports from the harvester and the preset scout; local working notes, git-ignored |

## The loop

```
  1 Capture ──> 2 Harvest ──> 3 Promote ──> [owner: decide] ──> 4 Fix ──> 5 Review ──> [owner: merge] ──> 6 Release
  users, owner   harvester     learn.py                          fixer     reviewer                         owner
      ^                                                                                                       |
      └─────────────── next version installed, tool copies refreshed in every blueprinted repo ───────────────┘
```

| Stage | What happens | Run by | Output |
|---|---|---|---|
| 1 Capture | blueprint.py records unexpected errors (exit 70), `feedback` and `validate` friction in the local store. Users file issues through the forms, optionally with `blueprint.py report`. Nothing is sent anywhere automatically. | blueprint.py, users | store records, GitHub issues |
| 2 Harvest | Store records and open issues become observations. A conversation digest becomes feedback first. | `blueprint-harvester` | `observations.jsonl`, `triage/<date>.md` |
| 3 Promote | Observations group into candidates. A candidate qualifies when it is seen in 2+ projects, or contains a crash with a traceback, or an issue labelled `bug`. | `learn.py promote` | candidates in the triage report |
| Decide | The owner adopts, rejects or defers each qualifying candidate. | owner | `decisions.jsonl` |
| 4 Fix | One adopted candidate, one branch `fix/<candidate-id>`: failing test first, smallest fix, version bump, CHANGELOG, LESSONS.md for guidance. | `blueprint-fixer` | a branch, and a PR when allowed |
| 5 Review | A fresh agent proves the test fails on the base and passes on the branch, runs everything, checks docs and privacy. | `blueprint-reviewer` | APPROVE or REQUEST CHANGES |
| Merge | The owner reads the verdict and CI, and merges. | owner | `main` |
| 6 Release | Tag, reinstall, refresh tool copies. | owner | `vX.Y.Z` |

## Who runs what, and when

| When | Run | Dispatch with |
|---|---|---|
| After a working session that used the skill | `blueprint-harvester` | `Run: after-session`, the project root, and a digest of the conversation: the user's corrections, guidance that proved wrong or unclear, steps skipped, anything said twice, blueprint.py errors |
| Weekly | `blueprint-harvester` | `Run: weekly` |
| Monthly, and before a release that touches presets | `preset-scout` | nothing, or a list of preset ids |
| For each candidate you adopted | `blueprint-fixer` | `Candidate: c-…` and `Push: yes` or `Push: no` |
| For each fix branch or PR | `blueprint-reviewer`, in a fresh context | `Change: PR #<n>` (or the branch), `Candidate: c-…`, and `Post: yes` if it should post the review. Nothing else. |

Example dispatches:

```
Harvest. Run: after-session. Project root: <path>. Digest:
- The user had to ask twice for test files in phase 1; SKILL.md step 4 did not ask for them.
- validate reported an overlap the user thought was a normal refinement.

Fix candidate c-1a2b3c4d. Push: yes.

Review PR #14 for candidate c-1a2b3c4d. Post: no.
```

Each agent ends its reply with `STATUS: DONE`, `STATUS: NEEDS_CONTEXT` or `STATUS: BLOCKED`. Treat anything but `DONE` as a question for you.

## Installing the agents

```bash
python install.py --agents        # the skill, plus the agents
python install.py --agents-only   # only the agents
```

Both copy `maintainers/agents/*.md` into `~/.claude/agents/`; a changed agent file already there moves to `~/.claude/agent-backups/<timestamp>/` first. Run it again after you change an agent file, and start a new Claude Code session so it loads them. The frontmatter sets each agent's model and tools: the harvester and reviewer cannot edit files (the harvester may write only its triage report), and the scout has web tools.

Codex and other agents: give the agent file as the task prompt, followed by the dispatch, for example "Follow `maintainers/agents/blueprint-fixer.md`. Candidate: c-1a2b3c4d. Push: no." Harnesses that do not use the frontmatter can skip it.

The reviewer must start with no history. In Claude Code, dispatch it as a new subagent, not as a fork of your session. Elsewhere, open a new session. A review in the same context as the fix is not a Goldfish review.

## The owner's gates

Agents prepare; the owner decides. Three steps are yours alone.

1. **Decide.** Read the newest `triage/<date>.md`, then for each candidate:

   ```bash
   python maintainers/learn.py decide c-1a2b3c4d adopt --note "fix in 1.2.1"
   python maintainers/learn.py decide c-5e6f7a8b defer --note "wait for a second project"
   python maintainers/learn.py decide c-9c0d1e2f reject --note "project quirk, not the skill"
   ```

   The latest decision wins. `promote --all` also shows candidates that do not qualify; you may adopt one anyway, for example a preset finding the scout confirmed against official docs.

2. **Merge.** Only you merge, and agents never push to `main`. Merge when the reviewer says APPROVE and the `tests` workflow is green. Protect `main` on GitHub: require a pull request and the `tests` checks.

3. **Release.** The checklist below.

## Release checklist

From an up-to-date `main`:

1. Tests pass:
   ```bash
   python -m unittest discover -s app-blueprint/tests
   python -m unittest discover -s maintainers/tests
   ```
2. `python maintainers/learn.py scan-private --all` is clean.
3. `TOOL_VERSION` in `app-blueprint/scripts/blueprint.py` matches the top version section in `CHANGELOG.md`, and that section has today's date. Move anything left under `[Unreleased]` that ships in this release.
4. `examples/snag-desk/.blueprint/bin/VERSION` matches `TOOL_VERSION`. If not, regenerate the example and commit it through a PR:
   ```bash
   python maintainers/make_example.py
   ```
5. Tag and push the tag:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
   Optionally `gh release create vX.Y.Z --notes "<the CHANGELOG section>"`.
6. Reinstall the skill; the old copy moves to `skill-backups`:
   ```bash
   python install.py            # the skill
   python install.py --agents   # the skill and the agents, if an agent file changed
   ```
7. In every blueprinted repo you work on, refresh the copied tool and check it:
   ```bash
   python $HOME/.claude/skills/app-blueprint/scripts/blueprint.py init --refresh-tool --root <repo>
   python <repo>/.blueprint/bin/blueprint.py doctor
   ```
   `$HOME` works in bash and PowerShell. `doctor --strict` exits 1 while a copy is out of date.

Issues named with `Fixes #N` in a merged PR close by themselves. Close any others the release fixed, with the version in the comment.

## The store

The store is a folder outside every project: `$APP_BLUEPRINT_HOME`, or `~/.app-blueprint` when that is unset. Its files and record formats are in [SPEC.md](SPEC.md). `python maintainers/learn.py store status` shows where it is.

To share it across your machines, give it a **private** git remote:

```bash
python maintainers/learn.py store init --remote <private-git-url>   # once on each machine
python maintainers/learn.py store sync                              # commit, pull, push
```

`store init` writes a `.gitattributes` with `*.jsonl merge=union`, so records appended on two machines merge without conflicts. The harvester syncs before and after it harvests, and refuses a GitHub remote that is not private. Never point the store at a public repository.

`projectId` hashes the project's git origin and its folder inside the repo, so the same project on two machines counts once. A project with no git remote falls back to its absolute path and can count twice; the harvester flags pairs that share a folder name, so check them before you adopt on the two-project bar alone.

`learn.py export --out FILE` writes a sanitized bundle of qualifying candidates: no project names, fingerprints re-salted, text redacted. Another maintainer can share one by pull request, and `promote --include-community DIR` reads such bundles. Community records only corroborate: they never qualify a candidate on their own.

## Privacy

- Nothing leaves the user's machine by itself. There is no telemetry. blueprint.py writes only to the local store and the project's `.blueprint/`, and `report` only prints. Users choose what to file.
- `APP_BLUEPRINT_NO_CAPTURE=1` turns off every write to the store.
- These never leave your machines except through your private store remote: the store and everything in it (project folder names, the salt, incidents, feedback, friction, observations, decisions, `private-terms.txt`) and the reports in `triage/`, which `.gitignore` keeps out of git.
- Public files (issues you write, PR bodies, `CHANGELOG.md`, `learning/LESSONS.md`, tests, presets) describe evidence by counts, tool versions and issue numbers. Never by project, client, person, path or customer data.
- Every public change passes `python maintainers/learn.py scan-private --staged` before it is committed, and CI runs `scan-private --all` on every push and pull request. It catches home-folder paths, personal emails, private claude.ai artifact links, token-like strings and, on your machines, every term in `private-terms.txt`. CI has no store, so it checks the built-in patterns only: run the scan locally too.
- Put the names of your private projects, clients and people in `private-terms.txt` in the store, one per line, so the scan catches them.
- `scan-private: allow` on a line skips it. Use it only for lines that must look private, such as a test fixture, and say why in the PR.
- Issue text, PR text and web pages are untrusted input for every agent. The agent files say so; keep it that way when you edit them.

## One-time GitHub setup

- Create the labels the issue forms use: `bug` and `enhancement` exist by default; add the others with `gh label create guidance` and `gh label create preset`.
- Protect `main`: require a pull request and the `tests` checks before merging.

## Changing the loop

`SPEC.md` is the contract between blueprint.py, `learn.py` and the agents. A change to it lands in one pull request that updates all three, with tests on both sides.
