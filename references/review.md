# Review and proof

What proves a claim, what each review lens asks, and how a review runs. Builders read the proof
standards before writing tests; paladins read all of it.

## Proof standards

Proportional to the claim: a mechanical edit needs its checks and CI; a behavior change needs
these.

1. **Values, not counts.** Tests check values, ordering and error paths, not only counts, status
   codes and happy paths.
2. **A failing test per fix:** the new test fails with the fix reverted (a job at the parent
   commit, or with the change reverted locally).
3. **A mutant per claim:** break the change once; a test must catch it.
4. **Sanitizers in separate builds:** ASan with UBSan, and TSan where threads are involved, each
   in its own build directory with its own dependency tree (a shared FetchContent directory links
   one flavor's libraries into another's binaries).
5. **An oracle or property test** where one exists: a reference implementation, a round trip, an
   invariant, an exhaustive small domain, a frozen copy of the old code.
6. **Asserts are live only in Debug.** A claim that an `assert` guards something needs a Debug
   build or the project's asserts-on option.
7. **Platform evidence names its platform.** A local TSan or libc++ result does not stand for
   another platform; CI covers the rest.

### Concurrency claims

All of: deterministic tests that force each interleaving the design names; a mutant per ordering
choice (swap two steps, drop a fence or a lock) that a test kills; stress against a sequential
model with at least two workers contending per object; separate TSan and ASan+UBSan builds. A
timing-dependent coverage check reports what it reached and never fails the run.

### Traps

- clang TSan links its runtime whole, so a test's own `operator new` never runs: observe
  allocations with `__sanitizer_malloc_hook`.
- g++ TSan "unexpected memory mapping" on Linux: run under `setarch -R`, or set
  `vm.mmap_rnd_bits=28`.
- Port-0 fixtures that bind, close and pass the port number race under parallel ctest: pass the
  open socket, or have the child report the port it bound.

## Lenses

The paladin carries every question the game master names; the lens decides the emphasis. For
critical quests and the plan challenge, the game master sends a paladin from another model family
when the harness offers one (`references/harness.md`).

| Lens | Asks |
|---|---|
| `correctness` | Does the code do what the quest file says on every path? Error paths free what they allocate and leave data consistent; tests fail without what they test; limits are load-bearing; scope stays inside the quest file; no second mechanism where one exists (one shape everywhere). |
| `security` | Who is the attacker (network peer, authenticated client, local user, malicious file, compromised dependency) and what do they gain? Input validated before use; bounds on lengths, depth, connections, memory; integer overflow in sizes and offsets; secrets out of logs and with tight modes; TLS and certificate checks; pinned dependencies. Probes feed truncated, oversized and malformed input to the real decoder; fuzz evidence is read, not assumed. |
| `durability` | What survives a crash at every step? Ordering of writes, flushes and renames; recovery from torn writes and partial files; behavior on full disk and I/O errors; concurrency of readers with writers; a crash matrix or executable model for the protocol, run as a job. |
| `design` | Before the code: is the shape right, are the decisions that are expensive to reverse (bytes on disk, wire format, public API, ordering protocol) settled and justified, is there a simpler mechanism, does every criterion have a rerunnable check? Rule once; an executable model settles disputes. |
| `system` | One question `campaign check` raised: two quests defining one contract differently, a contract changing under its consumers, one file with two writers, sequencing that assumes something unlanded. Name who acts. |

## How a review runs

1. **Start from the evidence.** List the criteria the journal's successful jobs already prove at
   the reviewed commit. Reuse those results: an identical job returns the same record.
2. **Probe the rest.** One probe per suspicion, as a job, kept under
   `.scratch/review-<ID>-<lens>/` in the home checkout so a fix can be confirmed by rerunning it.
   Build once with the sanitizer the risk needs; CI covers other platforms.
3. **Stop at what blocks landing.** List non-blocking notes in a line each.
4. **Write the review file** from `templates/review.md`: `Commit:`, `Verdict:` (APPROVE, CHANGES or
   REJECT), the evidence relied on, what was checked, a findings table with Blocking (yes or no) and
   Kind (defect, gap, nit), and "Not run".

## Confirming fixes

For each blocking finding the adventurer fixes, the game master reruns the paladin's probe (or the new test)
as a job at the fixed commit, and its revert check where the finding was a defect, then
`campaign accept ID LENS --jobs J1,J2`. No second review, unless the fix adds a mechanism or changes
an assumption: then one review scoped to the fix. After two fix rounds, stop and re-plan.
