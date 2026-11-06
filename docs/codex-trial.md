# Campaign 2.0: Codex field trial

Date: 2026-11-05. Host: macOS, Codex CLI 0.166.0. Branch: `campaign-2-codex`.

Dense pages is running under 2.0 as one high-risk quest with three complementary tasks. The migration preserves the owner's instruction to implement the complete feature before whole-feature reviews. No Dense pages landing or performance improvement is claimed yet.

## Migration

1. Parked three usage-limited contributors, committed their incomplete steps and ledgers, and drained holds. Checkpoints: Detent:c4fe881 full build, Detent:7fb6574 reads, Detent:64aab0f deltas, Detent:813c3ea lead integration. No running Dense command remained.
2. Installed from `origin/campaign-2` on a clean dotfiles checkout. No forced replacement was needed. Removed the obsolete lease symlink; install checks and tool help passed.
3. Ran all 66 tests: 313.469 seconds, passing. The fixed committed tree passed the same 66 tests as job `261105-150132-6cdfac` in 322.753 seconds. Ruff and strict Pyrefly are clean.
4. Adopted the full-build branch as `quest/dense-pages`, with reads, deltas and integration as task branches. Copied K1-K15 into A1-A15, retaining execution obligations and replacing obsolete per-step review language with the owner's whole-feature review instruction. Original branches, ledgers and reviews remain history.
5. Carried the conditional contract approval as a scoped historical review. It permits implementation and does not count as final design acceptance. `campaign check` reports no errors.
6. Relaunched full-build and reader adventurers from journals. Two explicit Astra/high launches were accepted. A third launch hit the harness's thread limit despite only two implementation agents running; a stale pending-init thread is visible. The delta journal remains ready for the next available slot.

Initialization began at 15:01 PST; the two implementation adventurers were active by 15:05. This is migration time, not quest-to-landing time. One short Scout launch and one retained-context followup exercised Sol/medium, background completion and resumption. Token accounting is unavailable; no token-saving estimate is made.

## What helps

One quest with tasks matches this format switch: components run in parallel and get one complete-feature review and landing. Jobs retain exact commits, commands and output independently of an agent's lifetime. The sidebar exposes running and queued work. Builds and model runs now use the queue instead of keeping developer turns alive for machine work.

Two jobs accidentally submitted at main were cancelled and replaced with explicit-SHA jobs. Their results do not count as candidate proof. The tool records make that mistake visible.

## Problems and corrections

| Finding | Correction or remaining limit |
|---|---|
| Pyrefly could not infer the empty status review list | Added `list[Json]`; strict check passes |
| Generator silently lowered max effort to high | Preserve max; added a generation/drift regression |
| Quiet CPU-load test killed children without reaping them | Wait for the killed children; the committed suite emits no leaked-subprocess warnings |
| Explicit `--quest ... --commit HEAD` from a task resolved HEAD in the home checkout | Resolve explicit refs in the submitting worktree of the same repository, including its default subdirectory; an executing regression passes |
| No CLI operation could correct an adopted task's file scope | Added `campaign set ID --task TASK files=GLOBS`, retaining overlap checks; regression covers rejection without state mutation |
| Migration guide describes one quest per old branch but not one feature split across branches | Used existing task adoption so complementary pieces keep one format-switch landing; recorded the mapping in the project plan |
| A bare APPROVE field cannot express conditional preimplementation approval | Preserve the original conditions in a separate scoped historical review; do not accept it as final design clearance |
| Generated-file location was presented as exercised Codex support | Harness notes distinguish documented schema/local symlinks from unexercised named loading |
| Task jobs default to the quest branch | Explicit task SHA is required for task evidence; documented in harness notes and journals |
| Deferred native repeat reused a build directory without a guaranteed configure | Direct check commands are provisional until the complete feature is qualified; deferred jobs must configure/build at their own exact landed commit |

The two task regressions passed as job `261105-150655-4d0d5a` in 6.557 seconds. Full-suite results above precede those two additions; targeted regression evidence accompanies them. Extended matrices and long soaks are deferred; native correctness, relevant sanitizers, bounded decoder proof and quantitative reader acceptance remain gates.

## Codex adaptations

The generated TOML schema and personal paths match the [official custom-agent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents). The installed skills and global guidance links match the [skills](https://learn.chatgpt.com/docs/build-skills) and [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) documentation. Named-type discovery requires a fresh session and remains unexercised. This session uses the generated instruction body in general agents.

`references/harness.md` records the dated runtime checks and class mapping: Astra for strongest roles; Sol/medium for Scout and Squire. The mapping is an operating choice, not a model benchmark. The shared SKILL methodology and Claude/Copilot generation remain unchanged.

A bounded `campaign wait --timeout 50` returned exit 3 while its job continued. The command waited through an exec session without model sampling between calls; billing telemetry is unavailable. `report` works with this cmux workspace. With cmux ids unset, a status operation exits successfully with empty output. This session exposes no recurring scheduler; report at state transitions and turn boundaries rather than inventing a scheduling capability.

## Next

Finish Dense pages builders, readers, deltas and safe old-format refusal/refetch. Reconcile the queued 100K/5M runs and native checks, then run the complete feature's correctness/performance and decoder-safety reviews before the architect's final merge ruling. Update this report with actual landing and check-wait times when they exist.
