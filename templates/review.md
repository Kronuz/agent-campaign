# {id} review: {lens}

Commit: <the full commit reviewed>
Verdict: <APPROVE | CHANGES | REJECT>
Date: <YYYY-MM-DD>

APPROVE: lands as is, at this commit's code. CHANGES: lands after each blocking finding is fixed
and confirmed by a job at the fixed commit. REJECT: the approach is wrong; the quest is re-planned.
An APPROVE lists no blocking finding; a CHANGES lists at least one.

## Evidence I relied on

<Job ids whose results I accepted, and why they are enough.>

## What I checked myself

<Probe jobs and mutants; their scripts under `.scratch/review-{id}-{lens}/`, to rerun on a fix.>

## Findings

| ID | Blocking | Kind | Finding | Evidence | Fix |
|---|---|---|---|---|---|

Blocking: yes or no. Kind: defect (wrong behavior), gap (unproven claim or missing test), nit. A
finding that would remove or narrow user-visible behavior offers options, including keeping it,
and is never blocking.

## Not run

<What I did not check, so nobody assumes I did.>
