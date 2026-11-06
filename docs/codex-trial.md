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

## Usage and quota follow-up (2026-11-05)

The extracted `agent-campaign` repository adds Codex readers on branch `codex`. The retired dotfiles branch had no unpublished changes. Its three commits were already on GitHub; no additional patch needs carrying over.

`campaign usage --json` now reads this campaign's rollout logs. A 21:09 PST snapshot reports:

| Logged run | Role/class from recorded assignment | Model / effort | Cumulative tokens | Active wall time |
|---|---|---|---:|---:|
| dense_fullbuild_v2 | Adventurer / ordinary implementation | Astra / high | 2,126,093 | 274.918 s |
| dense_reads_v2 | Adventurer / ordinary implementation | Astra / high | 2,666,200 | 340.396 s |
| dense_deltas_v2 | Adventurer / ordinary implementation | Astra / high | 216,171 | 36.629 s |
| codex_pace (this follow-up) | Adventurer / Squire | Sol / medium | 628,697 | 185.259 s |
| Game master, whole session | Lead / no class | Sol / medium | 68,025,214 | 5,302.766 s |

The role/class labels in this table come from the recorded assignments in this campaign's conversation. CLI output honestly marks these historical agent launches `unattributed`: this Codex installation encrypts their message bodies. Parent spawn and child agent-message shapes are supported when plaintext is available; the reader passes that text unchanged to `launch()`. It does not infer a class from a nickname. A scrubbed real-shape fixture exercises the plaintext case and encrypted fallback.

The game-master row includes work before adoption and this follow-up. Of its 68.03M tokens, 66,421,248 are cached input (97.6%). Cached input is a subset, not extra tokens; reasoning output is likewise not added twice. Codex supplies no monetary cost, so the tool reports cost unavailable as zero. These token totals cannot be converted into dollars or quota weights from the available logs.

For the actual resumed interval, 2026-11-05 23:01:00 to 23:11:00 UTC (15:01 to 15:11 PST), the same response records show:

| Role / class | Model / effort | Tokens | Share |
|---|---|---:|---:|
| Game master / no class | Sol / medium | 5,326,861 | 51.5% |
| Full-build adventurer / ordinary | Astra / high | 2,126,093 | 20.6% |
| Reader adventurer / ordinary | Astra / high | 2,666,200 | 25.8% |
| Delta adventurer / ordinary | Astra / high | 216,171 | 2.1% |
| Total | | 10,335,325 | 100% |

The interval is computed from timestamped `token_usage_record.usage` entries, deduplicated by response id, rather than adding cumulative `token_count` snapshots. Raw rollout content stays local. The first root quota sample in that interval was 14% at 23:01:00 UTC; the reader's quota reached 100% at 23:10:11 UTC. The window was already partly consumed. The logs do not identify how much quota each model's tokens cost, so blaming the exhaustion entirely on Astra would exceed the evidence.

The larger avoidable contributor is the lead's long, repeatedly read context: 5.33M tokens in ten minutes, including 5,256,064 cached input. Three simultaneous developers added 5.01M. The full-build developer produced passing targeted jobs; reader qualification was still incomplete, and delta hit the limit after 37 seconds. There is no comparable Sol implementation run proving a quality or efficiency advantage for either model.

The next experiment is one Sol/medium implementation adventurer with a compact journal assignment. Measure completed criteria, tokens and active time before expanding concurrency. Use Sol/high for a specific difficult design, optimization or review question; reserve Astra/high for an unresolved mechanism or independent ruling. Wizard/design-freeze max remains a bounded exception. Bard, Scout and Squire start at Sol/medium. The lead stays Sol/medium and must reduce its own context overhead. These recommendations replace the untested assumption that ordinary implementation should always use Astra/high.

`pace codex` reads both quota windows without network requests. At 21:10 PST it reported 5h 9% used, a 97.39 percentage-point/hour recent burn and exhaustion in 56 minutes from the sample, before reset at 2026-11-06 10:04 UTC. Weekly use was 65%, with a short-sample 11.92 point/hour burn and exhaustion before its reset. Percentages are quantized and the recent samples cover only minutes; these are forecasts, not guarantees. No further parallel launch follows that forecast.

The reader selects event timestamps across logs, ignores premium events with null windows, preserves reset identities, handles partial appended lines and caches offsets. Expired/stale observations are labeled. The shared skill gains no new instruction rule; its existing Usage row and Codex mapping carry the harness changes within the word budget.
