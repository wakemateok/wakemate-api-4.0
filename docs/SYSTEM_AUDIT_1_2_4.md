# System Audit 1.2.4

See the frontend release's `docs/system_audit_1_2_4.md` for the full user-facing scope and limits.

- Same-kind create/edit overlap checks run under a user-row lock before any invalidation.
- Model hours and personalized PVT day grouping use Taipei civil time, independent of timestamp offset.
- The existing daily recommendation budget is shared across windows by Taipei date; no cross-date merging.
- Earlier proposed doses affect later windows, while unissued past doses cannot consume future budget.
- Duplicate PVT data cannot repeatedly apply the trait EMA. PVT/parameter updates and source deletions
  participate in freshness checks.
- No coefficients, thresholds or parameter bounds change. Outputs can nevertheless differ from old
  versions because timezone interpretation and bookkeeping are corrected. Clinical review is required.
- The existing 300 mg budget is for new recommendations, not a new all-source clinical intake rule.
- No participant-data migration, historic deletion or automatic parameter reset is performed.
- The authenticated PVT response schema retains its intended per-owner personalization status,
  so clients/tests can distinguish a new update from an unchanged replay.

Tests include actual failed reproductions, 100 equivalent-instant comparisons, boundary/daily accounting,
trait replay prevention, atomic edit rejection, and the existing scoped-history/authentication suites.
