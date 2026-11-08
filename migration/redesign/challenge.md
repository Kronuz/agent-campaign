# Independent architecture challenge

> Status: CURRENT (2026-11-05). One review of `architecture.md` (first version) by a model of a
> different family (GPT-6.1, high effort), as the handoff required, before the expensive parts
> were built. Findings are summarized; the disposition column says what changed.

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| X1 | blocking | Acceptance kept by unchanged blobs misses upstream changes to helpers or assumptions outside the package's paths; blob ids omit file modes | **Partly accepted.** Blobs now carry the mode; the brief's blob is pinned (a changed brief voids acceptance); for high and critical packages, any code change on `main` between the accepted base and the landing base voids acceptance until the lead re-accepts after reading that diff. Normal and low packages keep blob identity: their checks rerun at the new head, which is the integration evidence. |
| X2 | blocking | The worker exits after dispatching the last job, so a dead runner's orphan is reaped only if someone reads the queue | **Accepted.** The worker lives while any job is queued or running, and reaps every tick. |
| X3 | blocking | `quiet` treats a hold as expired at its deadline while its command is still in the TERM grace, admitting the next measurement early | **Accepted.** A bounded hold's `until` includes a 15 s grace; the command is stopped at the deadline proper. |
| X4 | blocking | Ownership was declared, not enforced | **Accepted.** `start` refuses a package (or task) whose file globs overlap an active one in the same repository unless forced with a reason; `land` refuses code changes outside the package's globs. |
| X5 | blocking | Reclaim lacked attempt fencing; outputs were shared across attempts; pids may be reused after a reboot; retries assume idempotence | **Accepted.** Each attempt has its own directory; a runner writes its result only if it still owns the attempt; the boot id is recorded and a reaper never kills by a stale boot's ids; submit defaults to no automatic retry (checks queued by `verify` retry once). |
| X6 | important | No landing transaction: a crash after moving `main` loses the intended target | **Accepted.** `land` writes its intent to `state.json` before moving the ref; any later write command completes or clears it. The whole command already runs under one lock. |
| X7 | important | State commits on `main` break fast-forward and force reruns | **Already addressed, doc corrected.** Landing fast-forwards when it can and otherwise merges, provided `main`'s changes since the branch point are confined to `docs/campaign/`; the merged code tree is verified equal to the tested commit. |
| X8 | important | The evidence cache ignores mutable inputs (toolchain, data, stale slot artifacts) | **Accepted.** The job key includes a host toolchain fingerprint (`uname`, compilers, interpreters, plus `CAMPAIGN_FINGERPRINT`); critical packages' checks run in fresh checkouts, not slots. Unpinned external data must be named in the job's environment. |
| X9 | important | Two admission mechanisms do not give builds a turn: queued strict requests block light ones on macOS | **Accepted.** Fairness moved into `quiet`: a light request waiting 10 minutes marks itself starving; new strict holds wait for it, and it ignores queued (not running) strict requests. |
| X10 | important | Multi-repository packages lack integration semantics | **Accepted as scoped.** One package, one repository; cross-repository work is several packages with `needs`/`after` and an explicit landing order. Jobs may check out other repositories at exact commits (`--ref NAME=PATH@REF`), so an integration check pins the commit tuple. No atomic multi-repository landing is claimed. |
| X11 | important | The high-risk row could stack three reviews | **Accepted.** One reviewer by default, carrying all of the package's named risk questions; a second only for a named question that needs separate expertise, with the reason recorded. |
| X12 | important | Yield-based pruning could drop required security or durability scrutiny | **Accepted.** Yield-based dropping applies only to optional reviews (normal risk). |
| X13 | important | The synchronous fallback could not satisfy `land` | **Accepted.** `campaign job submit --sync` runs through the same runner and records the same result, without a worker. |

The reviewer also judged three roles and package-level review sound.
