# {id} review: {lens}

Commit: <the commit reviewed>
Verdict: <APPROVE | CHANGES | REJECT>
Date: <YYYY-MM-DD>

APPROVE: lands as is. CHANGES: lands after the blocking findings are fixed and the game master
has confirmed each fix by rerunning its probe. REJECT: the approach is wrong; the quest is
re-planned.

## Evidence I relied on

<Job ids and tests from the journal whose results I accepted, and why they are enough.>

## What I checked myself

<Probes, mutants, reruns; where they are (`.scratch/review-{id}-{lens}/`) and how to rerun them.>

## Findings

| ID | Blocking | Kind | Finding | Evidence | Fix |
|---|---|---|---|---|---|

Kind: defect (wrong behavior), gap (unproven claim or missing test), nit. A finding that would
remove or narrow user-visible behavior offers options, including keeping it, and is never
blocking.

## Not run

<What I did not check, so nobody assumes I did.>
