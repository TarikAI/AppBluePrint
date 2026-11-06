# Contributing

The most useful contribution is a report from real use: where the skill gave wrong or unclear guidance, where blueprint.py failed, or where a preset no longer matches its docs.

## Reporting

Open an issue with one of the [forms](https://github.com/TarikAI/AppBluePrint/issues/new/choose): bug, guidance, preset or feature. Issues are public, so remove project names, paths, customer data and secrets first.

If blueprint.py stops with exit code 70, it has recorded the error on your machine. Run

```bash
python .blueprint/bin/blueprint.py report
```

and paste the output into the bug form. It prints a redacted issue body and sends nothing. `blueprint.py doctor` shows your tool version, your project's copy of the tool and where the local store is.

To note a problem without filing an issue:

```bash
python .blueprint/bin/blueprint.py feedback --kind guidance --part "SKILL.md step 4" --note "..."
```

This stays on your machine. Nothing is ever sent automatically; `APP_BLUEPRINT_NO_CAPTURE=1` turns local recording off.

## How fixes flow

1. A lesson: an issue, or a candidate the maintainers' learning loop promoted (seen in two or more projects, a crash with a traceback, or an issue labelled `bug`). The owner decides which ones to fix.
2. A failing test first, committed before the fix.
3. The smallest fix, on its own branch, in a pull request that follows the template.
4. A review by someone (or an agent) with no context: the test must fail on the base and pass on the branch.
5. The owner merges and releases.

[maintainers/README.md](maintainers/README.md) describes the whole loop.

## Running the tests

```bash
python -m unittest discover -s app-blueprint/tests
python -m unittest discover -s maintainers/tests
```

On Windows, `py -3` works in place of `python`. CI runs both suites on Ubuntu and Windows with Python 3.10 and 3.12.

## The private-data scan

Before you commit:

```bash
git add <files>
python maintainers/learn.py scan-private --staged
```

It fails on home-folder paths, personal emails, private claude.ai artifact links and token-like strings. CI runs `scan-private --all`. Mark a line that must stay as it is (a test fixture, for example) with `scan-private: allow`, and say why in the pull request.

## Style

- Standard library only. No dependencies, ever.
- Python 3.10 or newer, on Windows, macOS and Linux.
- Plain, short sentences in docs and guidance. No marketing words.
- When behaviour changes, update `SKILL.md`, `README.md`, the references and the `--help` text in the same pull request.
- Bump `TOOL_VERSION` in `app-blueprint/scripts/blueprint.py` (patch for a fix, minor for a feature), add a `CHANGELOG.md` entry, and for guidance changes an entry in `learning/LESSONS.md`.

## The learning-loop contract

[maintainers/SPEC.md](maintainers/SPEC.md) defines the store, the record formats and the commands shared by `app-blueprint/scripts/blueprint.py`, `maintainers/learn.py` and the agents in `maintainers/agents/`. A change to SPEC.md must update all three in the same pull request.
