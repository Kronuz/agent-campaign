# {id} review: {lens}

Commit: <the full commit reviewed, or repo:commit>
Verdict: <APPROVE | CHANGES | REJECT>
Date: <YYYY-MM-DD>

APPROVE: lands as is, at this commit's code, with no blocking finding. CHANGES: lands once each
blocking finding's fix and its test pass the suite at the fixed commit; with only nits, an APPROVE.
REJECT: the approach is wrong; the quest is re-planned.

## Evidence I relied on

<Job ids whose results I accepted, and why they are enough.>

## What I checked myself

<Probe jobs; their scripts under `.scratch/review-{id}-{lens}/`, to rerun on a fix.>

## Findings

| ID | Blocking | Kind | Finding | Evidence | Fix |
|---|---|---|---|---|---|

Blocking: yes or no. Kind: defect (wrong behavior), gap (unproven claim or missing test), nit. A
finding that would remove or narrow user-visible behavior offers options, including keeping it,
and is never blocking.

## Not run

<What I did not check, so nobody assumes I did.>
