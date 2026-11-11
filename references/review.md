# Review and proof

What proves a claim, what each lens asks, and how a review runs. Builders read the proof
standards; reviewers read all of it.

## Proof standards

A mechanical edit needs the suite and CI; a behavior change needs these.

1. **Values, not counts.** Tests check values, ordering and error paths, not only counts, status
   codes and happy paths.
2. **A failing test per fix,** in the suite, run once: a revert or mutant job tagged `--for A1
   --fails-without` exits 0: it fails without the fix. Nothing else needs a mutant.
3. **An oracle or property test** where one exists: a reference implementation, a round trip, an
   invariant, an exhaustive small domain, a frozen copy of the old code.
4. **Asserts are live only in Debug.** A claim that an `assert` guards something needs a Debug
   build or the project's asserts-on option.
5. **Sanitizers and platforms are CI's,** each sanitizer in its own build directory and dependency
   tree (a shared FetchContent directory links one flavor's libraries into another's binaries). A
   local result does not stand for another platform.

### Concurrency claims

Deterministic tests that force each interleaving the design names, and stress against a sequential
model with at least two workers contending per object, run under TSan as the quest's one local
sanitizer gate. A timing-dependent coverage check reports what it reached and never fails the run.

### Traps

- clang TSan links its runtime whole, so a test's own `operator new` never runs: observe
  allocations with `__sanitizer_malloc_hook`.
- g++ TSan "unexpected memory mapping" on Linux: run under `setarch -R`, or set
  `vm.mmap_rnd_bits=28`.
- Port-0 fixtures that bind, close and pass the port number race under parallel ctest: pass the
  open socket, or have the child report the port it bound.

## Lenses

The reviewer carries every question the lead names; the lens decides the emphasis.

| Lens | Asks |
|---|---|
| `correctness` | Does the code do what the quest file says on every path? Error paths free what they allocate and leave data consistent; tests fail without what they test; limits are load-bearing; scope stays inside the quest file; no second mechanism where one exists (one shape everywhere). |
| `security` | Who is the attacker (network peer, authenticated client, local user, malicious file, compromised dependency) and what do they gain? Input validated before use; bounds on lengths, depth, connections, memory; integer overflow in sizes and offsets; secrets out of logs and with tight modes; TLS and certificate checks; pinned dependencies. Probes feed truncated, oversized and malformed input to the real decoder. |
| `durability` | What survives a crash at every step? Ordering of writes, flushes and renames; recovery from torn writes and partial files; behavior on full disk and I/O errors; concurrency of readers with writers; for a new on-disk or wire format or a changed durability mechanism, a crash model run as a job. |
| `design` | Before the code: is the shape right, are the decisions that are expensive to reverse (bytes on disk, wire format, public API, ordering protocol) settled and justified, is there a simpler mechanism, does every criterion have a rerunnable check? Rule once, with one text fix round; then probes run as jobs (an executable model, a test) settle disputes. |
| `system` | One question `campaign check` raised: two quests defining one contract differently, a contract changing under its consumers, one file with two writers, sequencing that assumes something unlanded. Name who acts. |

## How a review runs

1. **Start from the evidence.** `campaign status <ID>` lists each criterion with the jobs tagged for
   it. Do not rebuild what they prove, nor what CI runs.
2. **Probe the rest.** One probe per suspicion, as a job, from a script kept under
   `.scratch/review-<ID>-<lens>/` in the home checkout. Each blocking finding names the test its
   fix must add; the suite passing it confirms the fix, with no second review.
3. **Stop when the named risks are answered.** A consequential question you cannot settle is a
   blocking gap, not a reason to keep digging. List non-blocking notes in a line each.
4. **Write the review file** from `templates/review.md`: `Commit:` (the commit reviewed),
   `Verdict:` (APPROVE, CHANGES or REJECT), the evidence relied on, what was checked, a findings
   table (ID, Blocking yes or no, Kind defect, gap or nit), and "Not run". An APPROVE has no
   blocking row; a CHANGES whose findings are all nits is an APPROVE.
