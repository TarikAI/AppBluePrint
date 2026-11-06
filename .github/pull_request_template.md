## What and why

<!-- The problem, and what this change does about it, in two or three sentences. -->

Candidate: <!-- c-xxxxxxxx from `learn.py promote`, or "none" -->
Issue: <!-- Fixes #N -->

## Test

<!-- The test that failed before this change and passes now: its name, the command that runs it, and its failure on the base. For guidance with no automated test, the manual scenario and why no test is possible. -->

## Checklist

- [ ] Candidate id or issue link above
- [ ] Failing test first: committed before the fix, and shown failing on the base
- [ ] All tests pass: `python -m unittest discover -s app-blueprint/tests` and `python -m unittest discover -s maintainers/tests`
- [ ] `python maintainers/learn.py scan-private --staged` is clean
- [ ] `TOOL_VERSION` bumped in `app-blueprint/scripts/blueprint.py` (patch for a fix, minor for a feature)
- [ ] `CHANGELOG.md` entry
- [ ] `learning/LESSONS.md` entry, for changes to guidance (SKILL.md, references, presets)
- [ ] Docs match the new behaviour (SKILL.md, README.md, references, `--help`)
- [ ] Standard library only, Python 3.10+, no new dependencies
