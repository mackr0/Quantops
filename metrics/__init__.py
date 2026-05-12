"""Per-instrument-class metrics package — Phase 1 of the
instrument-class pipeline refactor (2026-05-11).

See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` for the full plan.

The legacy `metrics.py` module's contents now live in `legacy.py`
within this package. Every existing top-level import (`from metrics
import X`, `import metrics`) continues to work via the re-export
below — no consumer needs to change to use the legacy functions.

New code should import from the per-pipeline modules:
  - `metrics.stock`      — stock-only aggregations (filters
                           `WHERE occ_symbol IS NULL`).
  - `metrics.option`     — option-aware aggregations (slippage in $
                           not %, theta-decay-adjusted return,
                           gamma exposure).
  - `metrics.portfolio`  — cross-instrument aggregations that
                           genuinely span both (total equity, total
                           drawdown).

The audit (AUDIT_2026_05_11_AI_PIPELINE.md) finding #1 — the 1130%
slippage display bloat — is fixed by construction once consumers
move from `metrics.legacy.calculate_all_metrics` (which mixes
stock + option rows) to `metrics.stock.*` / `metrics.option.*`.
The legacy module remains for back-compat during migration.
"""
from .legacy import *  # noqa: F401,F403 — preserves public top-level
                       # imports (`from metrics import calculate_all_metrics`).

# Also re-export private helpers (underscore-prefix) that some tests
# import directly. `from X import *` deliberately skips these per
# Python convention; we re-export them explicitly so back-compat is
# total. Future cleanup: migrate those tests to import from
# `metrics.legacy` directly, then remove this block.
from . import legacy as _legacy_module
for _name in dir(_legacy_module):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_legacy_module, _name)
del _legacy_module, _name
