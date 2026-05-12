"""Pipeline registry — maps a profile/context to its enabled
pipelines.

Phase 0 contract: returns the list of `Pipeline` instances that
apply to a given context. The scheduler dispatcher will consume
this in Phase 1+ once it starts routing through pipelines.

Default policy (Phase 0): every profile evaluates both stocks AND
options unless explicitly disabled — matches today's behavior where
the AI proposes either across all profiles. Future profiles can
opt out via `ctx.disable_stock` or `ctx.disable_options`. Future
pipelines (Crypto, FX, Futures) will be added here as they're
implemented.
"""
from __future__ import annotations

from typing import List

from . import Pipeline
from .stock import StockPipeline
from .option import OptionPipeline


# Module-level singletons. Pipelines are stateless wrt the context
# (they receive ctx in every method) so one instance per kind suffices.
_STOCK_PIPELINE = StockPipeline()
_OPTION_PIPELINE = OptionPipeline()

# All known pipeline classes — used for tests + future runtime
# discovery. Keep in registration order; consumers shouldn't depend
# on order, but it's deterministic for snapshot testing.
ALL_PIPELINES: List[Pipeline] = [_STOCK_PIPELINE, _OPTION_PIPELINE]


def get_pipelines_for_profile(ctx) -> List[Pipeline]:
    """Return pipelines that apply to this profile's context.

    Filters `ALL_PIPELINES` by each pipeline's `applies_to(ctx)`.
    Phase 0: with no profile-level opt-out, every profile gets
    both stock and option pipelines.
    """
    return [p for p in ALL_PIPELINES if p.applies_to(ctx)]
