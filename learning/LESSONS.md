# Lessons

The public ledger of lessons the owner adopted. Each one changed the skill: its guidance, a preset, blueprint.py or the map. The loop that produces them is in [maintainers/README.md](../maintainers/README.md).

A lesson is added in the same pull request as the change it caused, by the fixer or by hand, newest last. Each entry has:

| Field | What it holds |
|---|---|
| id | `L-NNN`, never reused |
| date | the date the lesson was adopted |
| kind | `bug`, `guidance`, `preset` or `feature`, as in [maintainers/SPEC.md](../maintainers/SPEC.md) |
| part | the part of the skill, for example `SKILL.md step 5`, `preset:nextjs-prisma`, `command:sync`, `map:mind map` |
| lesson | the rule, in one or two sentences |
| evidence | what showed it: counts, tool versions, issue numbers, candidate id. Never project, client or person names, paths or customer data |
| change made | what changed, in which files and version |
| test | the test that fails without the change, or the manual check when no automated test is possible |

Every entry must pass `python maintainers/learn.py scan-private`.

## L-001: Anchor root-only files in owner rules

- **Date:** 2026-11-06
- **Kind:** guidance
- **Part:** `SKILL.md step 5` (owner rules), presets
- **Lesson:** An owner rule without a slash, such as `package.json`, matches that file name at any depth, so it takes every copy in the repo. Anchor root-only files with a leading `/`: `/package.json` is the root manifest only.
- **Evidence:** All four presets failed the preset validation test while their suggested owner rules used unanchored root-file patterns.
- **Change made:** The presets' owner rules anchor root files. SKILL.md step 5 and `references/schema.md` explain anchoring. Shipped in 1.0.0.
- **Test:** `app-blueprint/tests/test_presets.py`, `test_each_preset_validates`.

## L-002: Only partial overlaps need `override`

- **Date:** 2026-11-06
- **Kind:** guidance
- **Part:** `SKILL.md step 5` (owner rules), `command:validate`
- **Lesson:** Requiring `"override": true` on every later rule that overlaps an earlier one turned the overlap check into noise, because a narrower rule inside a broader one is the normal way to refine ownership. Only a partial overlap, a later rule that also takes paths the earlier rule does not cover, makes ownership depend on rule order.
- **Evidence:** The first design reported every overlap. Normal refinements, such as a fallback `**` followed by narrower rules, all needed `override`.
- **Change made:** `validate` reports only partial overlaps with an earlier rule of another owner, as an error unless the later rule has `"override": true`. SKILL.md step 5 and `references/schema.md` describe partial overlaps. Shipped in 1.0.0.
- **Test:** `app-blueprint/tests/test_blueprint.py`, `test_narrower_rules_need_no_override` and `test_partial_overlap_is_an_error_unless_override`.

## L-003: Fit the mind map inside the box padding, then measure again

- **Date:** 2026-11-06
- **Kind:** bug
- **Part:** `map:mind map`
- **Lesson:** Fitting the mind map to its box must measure the space inside the box padding. Fitting can make the map tall enough to add a vertical scrollbar, which narrows the box, so measure once more after the first fit.
- **Evidence:** Found while building the mind map view: after "fit", the map still showed a horizontal scrollbar.
- **Change made:** `fit` in `app-blueprint/assets/mindmap.js` measures inside the padding and re-measures once. Shipped in 1.1.0.
- **Test:** Manual check in `examples/snag-desk/.blueprint/map.html`: open the Mind map tab, press Expand all, then Fit, and confirm there is no horizontal scrollbar. There is no automated browser test yet.
