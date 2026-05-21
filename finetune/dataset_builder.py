"""Fine-tune dataset builder — corpus from the system's own outcomes.

Reads resolved predictions (live `ai_predictions` + the durable
`predictions_archive/` JSONL), filters to training-quality rows,
HINDSIGHT-RELABELS each to the action that would have been correct
given the realized outcome, and emits OpenAI-format chat JSONL with
a train / val / held-out-eval split.

The single most important invariant — pinned by
`tests/test_finetune_no_lookahead_bias.py` and asserted at runtime:
EVERY label is derived from an outcome that resolved STRICTLY AFTER
the prediction was made. A label that peeks at data from before (or
at) the decision time is look-ahead bias — it would make the model
look good in eval and be useless (or harmful) live. docs/20 §11
flags this as the one Critical-impact risk.

Design choices (docs/20 §5):
  - Hindsight relabel, NOT imitation: we train toward what was
    correct, not toward what the AI did. A losing BUY relabels to
    HOLD; a HOLD that left >5% on the table relabels to BUY.
  - Gray-zone skip: |return| in (2%, 5%) is ambiguous — excluded so
    the model isn't taught to chase marginal moves.
  - Cost-adjusted outcome (return_pct_net, #186) is preferred over
    gross when present — the label reflects what actually made money.

Granularity (first cut): one example per resolved prediction. The
user message is the exact `prompt_text` the AI saw; the assistant
message is the corrected single-candidate decision in the production
trade-dict shape so the live parser stays unchanged (docs/20 open
decision #6). The per-cycle batch-output variant (group by cycle_id,
emit all candidates' corrected actions in one example) is a noted
refinement once we measure whether per-candidate framing underfits.
"""
from __future__ import annotations

import json
import logging
import os
import random
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Outcome thresholds (absolute %). Mirror docs/20 §5.3.
_GRAY_ZONE_LO = 2.0   # below this magnitude → "flat" was fine
_STRONG_MOVE = 5.0    # above this → the missed direction was clearly right

_BULLISH_ACTIONS = frozenset({"BUY", "STRONG_BUY", "WEAK_BUY"})
_BEARISH_ACTIONS = frozenset({"SHORT", "STRONG_SELL", "SELL"})
# Option/multileg actions are NOT relabeled by this stock-outcome
# logic — their P&L is premium-based, not underlying-% based. They're
# filtered out (see _is_training_quality) and handled by a future
# option-specific corpus builder.
_OPTION_ACTIONS = frozenset({
    "OPTIONS", "MULTILEG_OPEN", "OPTION_EXERCISE", "PAIR_TRADE",
})


def hindsight_label(
    predicted_signal: str,
    actual_outcome: str,
    actual_return_pct: float,
    *,
    allow_short: bool = True,
) -> Optional[str]:
    """Return the hindsight-correct action for a resolved prediction,
    or None to SKIP the row (ambiguous gray-zone, or an action class
    this builder doesn't relabel).

    The label space is the stock directional set: BUY / SHORT / HOLD.
    Logic (docs/20 §5.3):

      directional entry that WON  → keep the action (it was right)
      directional entry that LOST → invert to HOLD (shouldn't have)
      HOLD with |return| < 2%     → HOLD (correctly stayed out)
      HOLD with return > +5%      → BUY  (missed the upside)
      HOLD with return < -5%      → SHORT (missed the downside;
                                    only if allow_short)
      anything in the 2-5% gray zone → None (skip; ambiguous)
      option/multileg actions       → None (premium P&L, not ours)

    `allow_short` reflects whether shorting is permitted; when False,
    a missed-downside HOLD relabels to HOLD (we wouldn't have shorted)
    rather than SHORT.
    """
    sig = (predicted_signal or "").upper()
    if sig in _OPTION_ACTIONS:
        return None
    outcome = (actual_outcome or "").lower()
    try:
        ret = float(actual_return_pct)
    except (TypeError, ValueError):
        return None
    mag = abs(ret)

    if sig in _BULLISH_ACTIONS or sig in _BEARISH_ACTIONS:
        # Directional entry. The resolver already classified win/loss
        # against the right per-direction criteria; trust it.
        if outcome == "win":
            # Normalize to the canonical directional label.
            return "BUY" if sig in _BULLISH_ACTIONS else "SHORT"
        if outcome == "loss":
            # The directional bet was wrong → staying flat was correct.
            return "HOLD"
        # neutral/scratch on a directional entry → ambiguous, skip.
        return None

    if sig == "HOLD":
        if mag < _GRAY_ZONE_LO:
            return "HOLD"  # correctly stayed out of a non-mover
        if ret >= _STRONG_MOVE:
            return "BUY"   # should have been long
        if ret <= -_STRONG_MOVE:
            return "SHORT" if allow_short else "HOLD"
        return None  # 2-5% gray zone — ambiguous

    # Unknown signal type — don't guess.
    return None


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp; return None on anything unparseable."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def assert_no_lookahead(row: Dict[str, Any]) -> None:
    """Raise AssertionError if a row's outcome did not resolve
    strictly AFTER the prediction was made.

    This is the corpus's load-bearing safety property. A label whose
    `resolved_at` is <= the prediction `timestamp` means the outcome
    was (or appeared) known at decision time — look-ahead bias. We
    refuse to emit such a row rather than silently poison the corpus.
    """
    pred_ts = _parse_ts(row.get("timestamp"))
    resolved_ts = _parse_ts(row.get("resolved_at"))
    # Both must be present and ordered. Missing resolved_at on a
    # status='resolved' row is itself suspect — treat as a violation.
    assert pred_ts is not None, (
        f"prediction id={row.get('id')} has no parseable timestamp"
    )
    assert resolved_ts is not None, (
        f"prediction id={row.get('id')} is resolved but has no "
        f"parseable resolved_at — cannot prove the label post-dates "
        f"the decision"
    )
    assert resolved_ts > pred_ts, (
        f"LOOK-AHEAD BIAS: prediction id={row.get('id')} "
        f"resolved_at={resolved_ts.isoformat()} is not strictly after "
        f"timestamp={pred_ts.isoformat()}. A label cannot be derived "
        f"from data known at or before decision time."
    )


def _is_training_quality(row: Dict[str, Any]) -> bool:
    """Filter (docs/20 §5.2). Only rows we can build a clean,
    non-leaking, unambiguous training example from."""
    if (row.get("status") or "").lower() != "resolved":
        return False
    prompt = row.get("prompt_text") or ""
    if len(prompt) <= 100:
        return False  # pre-B1 / stub rows
    if not row.get("raw_response_json"):
        return False
    if row.get("actual_return_pct") is None:
        return False
    if (row.get("actual_outcome") or "").lower() not in (
        "win", "loss", "scratch", "neutral",
    ):
        return False
    # data_quality tagged → corruption (e.g. tainted_equity); exclude.
    if row.get("data_quality"):
        return False
    # Option/multileg rows: premium P&L, not stock-% — out of scope
    # for this builder.
    if (row.get("predicted_signal") or "").upper() in _OPTION_ACTIONS:
        return False
    if row.get("occ_symbol"):
        return False
    return True


def _split_prompt(prompt_text: str,
                  delimiter: str = "PORTFOLIO STATE:") -> Tuple[str, str]:
    """Split the stored prompt into (system_prefix, user_body).

    The constant role/task preamble that `ai_analyst._build_batch_prompt`
    emits ends right before the dynamic per-cycle 'PORTFOLIO STATE:'
    block. Splitting there gives a stable system message (the role
    definition the model should always condition on) and a user
    message (the cycle-specific context). If the delimiter isn't
    found (prompt-builder changed), fall back to a generic system
    message + the whole prompt as the user body — still a valid
    training shape, just less factored.
    """
    idx = prompt_text.find(delimiter)
    if idx <= 0:
        return (
            "You are the apex portfolio-manager AI for an automated "
            "trading system. Decide each candidate's action.",
            prompt_text,
        )
    return prompt_text[:idx].rstrip(), prompt_text[idx:]


def _corrected_assistant_message(
    row: Dict[str, Any], label: str,
) -> str:
    """Build the assistant (target) message: the hindsight-correct
    decision for this candidate, in the production trade-dict shape so
    the live parser stays unchanged (docs/20 open decision #6).

    Starts from the candidate's own trade dict in raw_response_json
    when present (to preserve sizing/target shape), overrides `action`
    to the hindsight label, and — when the label is HOLD — zeroes the
    sizing/targets since a HOLD takes no position.
    """
    symbol = row.get("symbol")
    base: Dict[str, Any] = {"symbol": symbol, "action": label}
    # Try to recover the original per-candidate dict for shape.
    try:
        resp = json.loads(row.get("raw_response_json") or "{}")
        trades = resp.get("trades") if isinstance(resp, dict) else None
        if isinstance(trades, list):
            for t in trades:
                if isinstance(t, dict) and t.get("symbol") == symbol:
                    base = dict(t)
                    base["action"] = label
                    break
    except (ValueError, TypeError):
        pass
    if label == "HOLD":
        # A HOLD opens nothing — strip sizing/targets so the model
        # doesn't learn to attach position params to a no-op.
        for k in ("size_pct", "stop_loss_pct", "take_profit_pct",
                  "strategy_name", "strikes", "expiry", "contracts"):
            base.pop(k, None)
        base["size_pct"] = 0
    return json.dumps({"trades": [base]}, separators=(",", ":"))


def build_example(row: Dict[str, Any], *,
                  allow_short: bool = True) -> Optional[Dict[str, Any]]:
    """Transform one resolved prediction into an OpenAI chat example,
    or None to skip. Asserts no-look-ahead on every emitted row.

    Prefers the cost-adjusted net return (#186) for the outcome
    magnitude when present — the label should reflect what actually
    made money after costs, not the gross price move.
    """
    if not _is_training_quality(row):
        return None
    ret = row.get("actual_return_pct_net")
    if ret is None:
        ret = row.get("actual_return_pct")
    label = hindsight_label(
        row.get("predicted_signal"),
        row.get("actual_outcome"),
        ret,
        allow_short=allow_short,
    )
    if label is None:
        return None
    # Load-bearing safety check — refuse leaking rows.
    assert_no_lookahead(row)
    system_msg, user_msg = _split_prompt(row.get("prompt_text") or "")
    return {
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
            {"role": "assistant",
             "content": _corrected_assistant_message(row, label)},
        ],
        # Carried for the dataset builder's weighting/split — NOT part
        # of the OpenAI training line (stripped before write).
        "_meta": {
            "id": row.get("id"),
            "timestamp": row.get("timestamp"),
            "symbol": row.get("symbol"),
            "label": label,
            "return_pct_net": ret,
        },
    }


def _iter_live_rows(profile_db: str) -> Iterable[Dict[str, Any]]:
    """Yield resolved ai_predictions rows from a profile journal."""
    import sqlite3
    try:
        with closing(sqlite3.connect(profile_db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM ai_predictions WHERE status = 'resolved'"
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("dataset_builder: read %s failed: %s",
                       profile_db, exc)
        return
    for r in rows:
        yield dict(r)


def _iter_archive_rows(archive_root: str) -> Iterable[Dict[str, Any]]:
    """Yield resolved rows from predictions_archive/*/*/predictions.jsonl."""
    root = Path(archive_root)
    if not root.exists():
        return
    for jsonl in root.glob("*/*/predictions.jsonl"):
        try:
            with open(jsonl) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except (ValueError, TypeError):
                        continue
        except OSError as exc:
            logger.warning("dataset_builder: archive read %s failed: %s",
                           jsonl, exc)


def build_dataset(
    profile_dbs: List[str],
    out_dir: str,
    *,
    archive_root: Optional[str] = "predictions_archive",
    allow_short: bool = True,
    eval_holdout: int = 200,
    val_fraction: float = 0.10,
    seed: int = 1729,
) -> Dict[str, Any]:
    """Build train/val/eval OpenAI JSONL files from live + archived
    predictions across the given profile journals.

    Dedups by prediction id across live + archive (the same id can
    appear in both if a row was archived then re-resolved). Holds out
    the most recent `eval_holdout` examples (by prediction timestamp)
    as a self-scored eval set NOT given to the vendor. Splits the rest
    90/10 train/val.

    Returns a manifest dict: counts + file paths.
    """
    seen_ids = set()
    examples: List[Dict[str, Any]] = []

    def _ingest(rows: Iterable[Dict[str, Any]]):
        for row in rows:
            rid = row.get("id")
            key = rid if rid is not None else id(row)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            ex = build_example(row, allow_short=allow_short)
            if ex is not None:
                examples.append(ex)

    for db in profile_dbs:
        _ingest(_iter_live_rows(db))
    if archive_root:
        _ingest(_iter_archive_rows(archive_root))

    # Most-recent-first by prediction timestamp for the eval holdout.
    examples.sort(
        key=lambda e: e["_meta"].get("timestamp") or "",
        reverse=True,
    )
    eval_set = examples[:eval_holdout]
    train_pool = examples[eval_holdout:]

    rng = random.Random(seed)
    rng.shuffle(train_pool)
    n_val = int(len(train_pool) * val_fraction)
    val_set = train_pool[:n_val]
    train_set = train_pool[n_val:]

    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for name, dataset in (("train", train_set), ("val", val_set),
                          ("eval", eval_set)):
        path = os.path.join(out_dir, f"{name}.jsonl")
        with open(path, "w") as fh:
            for ex in dataset:
                # Strip the _meta channel — only messages go to the vendor.
                fh.write(json.dumps({"messages": ex["messages"]}) + "\n")
        paths[name] = path

    # The eval set keeps its _meta sidecar for self-scoring.
    eval_meta_path = os.path.join(out_dir, "eval_meta.jsonl")
    with open(eval_meta_path, "w") as fh:
        for ex in eval_set:
            fh.write(json.dumps(ex["_meta"]) + "\n")
    paths["eval_meta"] = eval_meta_path

    manifest = {
        "total_examples": len(examples),
        "train": len(train_set),
        "val": len(val_set),
        "eval": len(eval_set),
        "paths": paths,
        "label_distribution": _label_dist(examples),
    }
    logger.info("dataset_builder: built corpus %s", manifest)
    return manifest


def _label_dist(examples: List[Dict[str, Any]]) -> Dict[str, int]:
    dist: Dict[str, int] = {}
    for e in examples:
        lbl = e["_meta"].get("label", "?")
        dist[lbl] = dist.get(lbl, 0) + 1
    return dist
