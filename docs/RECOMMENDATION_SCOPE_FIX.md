# Recommendation Scope Fix

Past sleep/wake conflicts were blocking new recommendations because every active historical
period was checked. Those old records can remain useful research data and must not be deleted
or silently changed to make a new calculation succeed.

## Rule

- Recommendation validation checks wake windows whose end is strictly after the current instant.
- All effective sleep records remain available when checking those windows and computing the model.
- Ongoing cross-midnight shifts are included even when their start date is yesterday.
- An ended wake window, including one ending exactly now, cannot block a new calculation/read.
- New or edited inputs still reject actual overlaps atomically, including historical inputs.
- Deleted, invalidated and inactive records retain their existing exclusion behavior.

The same scope is used by API validation, save-triggered calculations, scheduled core calculations,
alertness generation and the frontend preflight. No dose constants, model equations or personal
parameters were changed. Core recommendation loops skip expired target windows; their historical
records remain intact. Alertness history remains available.

Explicit manual retries bypass processed-source optimization for both derived outputs. Per-user
Postgres advisory locks serialize recommendation replacement across devices, leaving only the
latest active result while retaining inactive historical runs.

## Verification

Regression coverage includes a past conflict plus valid future inputs, manual retry after an
already-processed source, relevant-conflict IDs, cross-midnight and exact-end boundaries, and
100 numerical comparisons with/without unrelated historical wake conflicts.
Cloud smoke testing uses only a newly created synthetic account and never edits real participants.
