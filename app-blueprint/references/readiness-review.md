# Goldfish readiness review

Give this brief to a **fresh** subagent or a new session with no conversation history. Give it only the three files named below and the repo for reading. The point is that it knows nothing the plan does not say.

The method comes from Google's Elephant-Goldfish Model (zero-context reviewers, the Zero-Decision Rule and the Zoom-Out standard). EGM itself is not required.

---

**Brief to paste:**

You are a read-only reviewer. You have not seen any discussion about this project. Do not edit files.

Read `.blueprint/blueprint.json`, `.blueprint/STRUCTURE.md` and `.blueprint/ROSTER.md`. You may read existing files in the repo to check claims about them. Then answer, with evidence from those files:

1. **Zero decisions.** For each phase-1 file and each shared contract: could a competent engineer who has never seen this project build it from its entry and the contracts it names, without choosing a schema, an API path or shape, a library, a file location, or how to handle an edge case? List each file where they would have to choose, and what they would have to choose.
2. **Contracts line up.** Does every `consumes` name match a `produces` name exactly? Is any interface used by two owners but not marked as a contract?
3. **Walking skeleton.** Does P1 alone build, run and test something end to end? Name anything missing (an entry point, config, a test, a migration, the wiring between layers).
4. **Order.** Can each phase be built using only earlier phases? Is any dependency missing from `dependsOn`?
5. **Ownership.** Will two owners need to edit the same file to finish their work? Is any shared contract owned by a builder instead of a lead? Are any owner rules surprising?
6. **Patterns.** Is each pattern's `instance` specific enough to build one instance without asking?
7. **Lean and conventional.** Any speculative folder, layer or abstraction not needed by a planned feature? Anything custom where the stack has a standard convention (check the `docs` URLs in `stack`)? Anything placed in the wrong layer?
8. **Verification.** Does every file have a verify command that would actually fail if the file were wrong?

Report each finding as:

- **Severity:** Blocker (an agent cannot build correctly), Major (likely rework), or Nit
- **Where:** file path, pattern id, module or rule number
- **Problem:** one or two sentences
- **Fix:** the specific change to the blueprint

End with one line: `READY`, or `NOT READY: <n> blockers`. No other commentary.

---

After the review: show the findings to the user, apply the fixes they accept, then run `validate` and `render` again. Run a second review only if blockers were found.
