"""Per-pipeline specialist routing (Phase 4 of the pipeline refactor).

Each pipeline owns its own specialist set. Stock proposals go through
stock-aware specialists; option proposals go through option-aware
specialists. A pipeline's `route_to_specialists()` calls
`applicable_specialists(self.name)` to get the filtered module list,
then hands it to `ensemble.run_ensemble(specialists_override=...)`.

Closes audit findings:
  #5 — multileg trades bypassed risk_assessor (which reads stock-
       shaped 1:1 exposure). Now multileg routes through
       `option_spread_risk` which reads max-loss-at-expiry × 100.
  #6 — stock specialists like `pattern_recognizer` fired on option
       proposals and produced noise (option contract chart patterns
       are meaningless when premiums move on Greeks). Now they only
       fire on stock proposals.

Tagging contract:
- Each specialist module declares an `APPLIES_TO_PIPELINES` tuple of
  pipeline names: ("stock",), ("option",), or ("stock", "option").
- Modules WITHOUT the tag are treated as ("stock",) for back-compat
  with anything that pre-dates the refactor and predates option
  routing — preserves the original system's behavior on stock
  proposals while keeping option proposals safe from untagged
  legacy modules until they're audited.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from specialists import discover_specialists


_DEFAULT_PIPELINES: Tuple[str, ...] = ("stock",)


def _module_pipelines(spec_module: Any) -> Tuple[str, ...]:
    """Return the pipelines this specialist applies to.

    Reads the module's `APPLIES_TO_PIPELINES` tuple. Defaults to
    ("stock",) for legacy modules that haven't been tagged yet —
    safe default because the system was stock-only before the
    refactor and any unaudited specialist could only have been
    designed for stock proposals.
    """
    tag = getattr(spec_module, "APPLIES_TO_PIPELINES", None)
    if not tag:
        return _DEFAULT_PIPELINES
    if isinstance(tag, str):
        return (tag,)
    return tuple(tag)


def applicable_specialists(pipeline_name: str) -> List[Any]:
    """Return the specialist modules tagged for this pipeline.

    Pure function — same input always produces the same output
    given the current specialist registry. Tests can call this
    directly without touching AI providers.
    """
    if not pipeline_name:
        return []
    return [
        spec for spec in discover_specialists()
        if pipeline_name in _module_pipelines(spec)
    ]


def applicable_specialist_names(pipeline_name: str) -> List[str]:
    """Convenience: just the NAMEs, in registry order. Useful for
    logging which specialists ran for a given pipeline cycle."""
    return [s.NAME for s in applicable_specialists(pipeline_name)]
