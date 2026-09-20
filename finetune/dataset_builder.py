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

Granularity: one example per CYCLE (`build_cycle_example`). The user
message is the exact `prompt_text` the AI saw; the assistant message
is the corrected action set for every labeled candidate of that cycle
in the production trade-dict shape, HOLD expressed by omission. The
per-prediction `build_example` is kept as a primitive only — per-row
targets taught a degenerate one-pick convention (batch 1).

Three build-time guarantees added for batch 4 (docs/28):
  - LENGTH: no example longer than the training window is ever
    written. The trainer truncates the END of a long sequence — which
    is where the answer is — so an over-length example trains on no
    answer at all (31-40% of batches 1-3). Over-length examples are
    split along the candidate table; unsplittable ones are dropped
    and counted.
  - TARGET HYGIENE: internal bookkeeping keys (leading underscore,
    e.g. `_ledger_rar`) never reach a target — the production AI
    never emits them.
  - REBALANCE: the TRAIN split only is reshaped (empty-target cap,
    direction balance); val and eval keep the natural distribution.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
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


def _base_quality(row: Dict[str, Any]) -> bool:
    """Shared quality checks (docs/20 §5.2): resolved, real prompt,
    real response, real outcome, untainted."""
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
    # A cycle whose AI call FAILED (provider 429/5xx) or was cost-capped
    # still journals a HOLD prediction for every candidate — decisions
    # the model never made (8,187 of them in Experiment 2's first four
    # weeks). They used to stay out of the corpus only by accident
    # (such cycles store no prompt). Excluded on purpose.
    if _is_failed_call(row.get("raw_response_json")):
        return False
    return True


_FAILED_CALL_MARKERS = ("AI call failed", "Cost cap reached")


def _is_failed_call(raw_response_json: Any) -> bool:
    """True when the stored response is `ai_analyst`'s own stand-in for
    a call that never produced an answer."""
    raw = raw_response_json or ""
    if not isinstance(raw, str) or not any(
            m in raw for m in _FAILED_CALL_MARKERS):
        return False
    try:
        resp = json.loads(raw)
    except (ValueError, TypeError):
        # Unparseable AND carrying a failure marker: not an answer.
        return True
    if not isinstance(resp, dict):
        return False
    if resp.get("cost_capped"):
        return True
    reasoning = str(resp.get("portfolio_reasoning") or "")
    return (not resp.get("trades")
            and reasoning.startswith(_FAILED_CALL_MARKERS))


def _is_option_row(row: Dict[str, Any]) -> bool:
    return bool(row.get("occ_symbol")) or (
        (row.get("predicted_signal") or "").upper() in _OPTION_ACTIONS)


def _is_training_quality(row: Dict[str, Any]) -> bool:
    """Stock-path filter. Option/multileg rows are labeled by the
    PREMIUM-based option labeler (2026-08-27), never by stock-% logic."""
    if not _base_quality(row):
        return False
    if _is_option_row(row):
        return False
    return True


# Option hindsight thresholds (2026-08-27) — PREMIUM-based, grounded
# in the archive's measured outcome distribution (p50 −95.4%, p75
# +28.7%, p90 +108%): a decision that kept ≥ +20% of premium was
# right (keep the action); one that lost ≥ 20% of premium should not
# have been taken (label HOLD → omitted from the cycle target); the
# band between is ambiguous and skipped. actual_return_pct on option
# rows IS the premium return (verified: CVX put −97.1% while the
# underlying moved −2.5%), so no stock-% contamination is possible.
_OPT_WIN_PCT = 20.0
_OPT_LOSS_PCT = -20.0


def option_hindsight_label(predicted_signal: str,
                           premium_return_pct: float) -> Optional[str]:
    """Hindsight label for an option/multileg decision from its
    premium return: keep the original action on a clear win, HOLD on a
    clear loss, None (skip) in the ambiguous band."""
    if premium_return_pct >= _OPT_WIN_PCT:
        return (predicted_signal or "OPTIONS").upper()
    if premium_return_pct <= _OPT_LOSS_PCT:
        return "HOLD"
    return None


ORIGINS = ("kept_win", "lost_entry", "flat_hold", "missed_move")


def label_origin(predicted_signal: str, label: str) -> str:
    """Where a hindsight label came from — the AI's own signal plus
    the label it was corrected to fully determine it:

      kept_win     the AI entered and the entry won (label = its action)
      lost_entry   the AI entered and lost (label HOLD) — the valuable
                   "this looked good and wasn't" lesson
      flat_hold    the AI said HOLD and HOLD was right (incl. a missed
                   downside when shorting isn't allowed)
      missed_move  the AI said HOLD and the stock then moved >5% — the
                   noisiest labels (was that move knowable?)

    Carried in `_meta` only; never written to a training file. The
    rebalancer uses it to prefer losing-entry empties over flat ones,
    and the manifest reports its distribution (docs/28 §4.1)."""
    entered = (predicted_signal or "").upper() != "HOLD"
    if label == "HOLD":
        return "lost_entry" if entered else "flat_hold"
    return "kept_win" if entered else "missed_move"


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


def _corrected_trade_dict(
    row: Dict[str, Any], label: str,
) -> Dict[str, Any]:
    """The hindsight-correct trade dict for one candidate, in the
    production shape (docs/20 open decision #6). Starts from the
    candidate's own dict in raw_response_json when present (to
    preserve sizing/target shape) and overrides `action` to the
    hindsight label."""
    symbol = row.get("symbol")
    base: Dict[str, Any] = {"symbol": symbol, "action": label}
    # Try to recover the original per-candidate dict for shape.
    try:
        resp = json.loads(row.get("raw_response_json") or "{}")
        trades = resp.get("trades") if isinstance(resp, dict) else None
        if isinstance(trades, list):
            for t in trades:
                if isinstance(t, dict) and t.get("symbol") == symbol:
                    # Leading-underscore keys are internal bookkeeping
                    # stamped onto the stored response AFTER the AI
                    # answered (opportunity_ledger's `_ledger_rar`,
                    # `_ledger_best_rar`, …). The production AI never
                    # emits them; 18% of batch-3 target trades carried
                    # them, teaching the model to invent ledger fields.
                    base = {k: v for k, v in t.items()
                            if not str(k).startswith("_")}
                    base["action"] = label
                    break
    except (ValueError, TypeError) as exc:
        # The stored response is only a SHAPE donor (sizing/targets);
        # without it the target is the bare symbol+action, which is
        # still a correct label. Logged so a corrupt response is seen.
        logger.debug("dataset_builder: raw_response_json unusable for "
                     "%s (id=%s): %s", symbol, row.get("id"), exc)
    if label == "HOLD":
        # A HOLD opens nothing — strip sizing/targets so the model
        # doesn't learn to attach position params to a no-op.
        for k in ("size_pct", "stop_loss_pct", "take_profit_pct",
                  "strategy_name", "strikes", "expiry", "contracts"):
            base.pop(k, None)
        base["size_pct"] = 0
    return base


def _corrected_assistant_message(
    row: Dict[str, Any], label: str,
) -> str:
    """Single-candidate assistant target (the original per-prediction
    granularity — kept as a primitive; production-shape training uses
    the cycle-grouped variant below)."""
    return json.dumps({"trades": [_corrected_trade_dict(row, label)]},
                      separators=(",", ":"))


def build_cycle_example(
    group: List[Tuple[Dict[str, Any], str, float]],
) -> Optional[Dict[str, Any]]:
    """One example per CYCLE (2026-08-27, docs/20 §5's "noted
    refinement", forced by batch-1 evidence): the production prompt
    asks for a BATCH of candidates and omits non-actionable names, so
    the target must be the corrected action set for ALL of the cycle's
    labeled candidates — non-HOLD labels as trade dicts, HOLD labels
    via omission. Per-prediction targets taught the adapter a
    degenerate one-pick convention (batch-1 eval: 44/50 answers were a
    single trade or nothing).

    `group` is [(row, label, ret), ...] sharing one prompt. Every row
    re-passes the look-ahead guard here (load-bearing)."""
    if not group:
        return None
    for row, _label, _ret in group:
        assert_no_lookahead(row)
    first = group[0][0]
    system_msg, user_msg = _split_prompt(first.get("prompt_text") or "")
    trades = [
        _corrected_trade_dict(row, label)
        for row, label, _ret in sorted(
            group, key=lambda g: str(g[0].get("symbol") or ""))
        if label != "HOLD"
    ]
    labels = {str(row.get("symbol")): label for row, label, _ret in group}
    returns = {str(row.get("symbol")): ret for row, _label, ret in group}
    origins = {str(row.get("symbol")):
               label_origin(row.get("predicted_signal"), label)
               for row, label, _ret in group}
    symbol_ids: Dict[str, List[Any]] = {}
    for row, _label, _ret in group:
        symbol_ids.setdefault(str(row.get("symbol")), []).append(
            row.get("id"))
    return {
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
            {"role": "assistant",
             "content": json.dumps({"trades": trades},
                                   separators=(",", ":"))},
        ],
        "_meta": {
            "cycle_id": first.get("cycle_id"),
            "ids": [row.get("id") for row, _l, _r in group],
            "timestamp": max((row.get("timestamp") or ""
                              ) for row, _l, _r in group),
            # When the LAST of this cycle's labels became known — the
            # purge between train / val / eval keys on it.
            "resolved_at": max((str(row.get("resolved_at") or "")
                                ) for row, _l, _r in group),
            "labels": labels,
            "returns": returns,
            "origins": origins,
            "symbol_ids": symbol_ids,
        },
    }


def _label_and_return(row: Dict[str, Any], *, allow_short: bool = True
                      ) -> Optional[Tuple[str, float]]:
    """Quality-filter + hindsight-label one row: (label, net-preferred
    return) or None to skip. Shared by the per-row and cycle-grouped
    example builders. Stock rows label by underlying-% (hindsight_label);
    option rows by PREMIUM return (option_hindsight_label, 2026-08-27 —
    operator: options are half the system, they train too)."""
    if not _base_quality(row):
        return None
    ret = row.get("actual_return_pct_net")
    if ret is None:
        ret = row.get("actual_return_pct")
    if _is_option_row(row):
        label = option_hindsight_label(
            row.get("predicted_signal"), float(ret))
    else:
        label = hindsight_label(
            row.get("predicted_signal"),
            row.get("actual_outcome"),
            ret,
            allow_short=allow_short,
        )
    if label is None:
        return None
    return label, ret


def build_example(row: Dict[str, Any], *,
                  allow_short: bool = True) -> Optional[Dict[str, Any]]:
    """Transform one resolved prediction into an OpenAI chat example,
    or None to skip. Asserts no-look-ahead on every emitted row.

    Prefers the cost-adjusted net return (#186) for the outcome
    magnitude when present — the label should reflect what actually
    made money after costs, not the gross price move.
    """
    labeled = _label_and_return(row, allow_short=allow_short)
    if labeled is None:
        return None
    label, ret = labeled
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
            "origin": label_origin(row.get("predicted_signal"), label),
        },
    }


# ---------------------------------------------------------------------------
# Length control — nothing longer than the training window is written
# ---------------------------------------------------------------------------
#
# mlx-lm keeps the FIRST max_seq_length tokens of a long sequence and
# drops the rest; the answer is the last few dozen tokens, so an
# over-length example trains on no answer at all. Measured 2026-09-20
# with the real Qwen tokenizer: 30.8% of batch-2 and 39.8% of batch-3
# training examples were over the 8,192 window, the answer cut off
# entirely in 279 of 282. With prompt masking on, such an example has
# ZERO loss tokens and the trainer's loss divides by zero.

DEFAULT_MAX_TOKENS = 8192
# Conservative characters-per-token for builds with no tokenizer (the
# droplet). Calibrated 2026-09-20 on 800 real examples with the Qwen2.5
# tokenizer: min 2.68, 5th percentile 2.76, median 3.02. 2.6 sits
# below the minimum, so the estimate can only OVER-count tokens — it
# may split an example that would have fit, never pass one that won't.
_CHARS_PER_TOKEN = 2.6
# Chat-template framing (role markers, turn delimiters) for 3 messages.
_CHAT_OVERHEAD_TOKENS = 32
# Slack when packing candidate blocks: token counts of separately
# counted fragments are not exactly additive across joins.
_PACK_MARGIN_TOKENS = 96


class CharEstimateCounter:
    """Tokenizer-free length estimate. `finetune.local_train` swaps in
    an exact counter built on the real tokenizer when one is
    installed (the Mac); both expose `.method`, `.text()`,
    `.messages()`."""

    method = f"estimate:{_CHARS_PER_TOKEN}-chars-per-token"

    def text(self, s: str) -> int:
        return math.ceil(len(s or "") / _CHARS_PER_TOKEN)

    def messages(self, messages: List[Dict[str, str]]) -> int:
        return (sum(self.text(m.get("content") or "") for m in messages)
                + _CHAT_OVERHEAD_TOKENS)


# The candidate table's grammar, as `ai_analyst._build_batch_prompt`
# emits it. The region runs from the first section header to the
# "RULES:" block. Verified over all 9,450 batch-3 training prompts:
# every top-level (column-0) line inside the region is one of these —
# anything else means the prompt builder changed, and the splitter
# refuses rather than guess where a candidate ends.
_REGION_START = re.compile(
    r"^(?:LONG CANDIDATES \(|SHORT CANDIDATES|CANDIDATES \(ranked"
    r"|DISCRETIONARY WATCH )", re.M)
_REGION_END = "\n\nRULES:\n"
_SECTION_HEADER = re.compile(
    r"^(?:LONG CANDIDATES \(|SHORT CANDIDATES|CANDIDATES \(ranked"
    r"|DISCRETIONARY WATCH )")
_CANDIDATE_START = re.compile(r"^ {2}\d+\. (\S+) @ ")
_CANDIDATE_OWNED = re.compile(
    r"^(?:SIMILAR PAST CASES \(.*\) FOR (\S+):"
    r"|DETERMINISTIC RULE PANEL FOR (\S+) \()")
_SECTION_FOOTER = re.compile(r"^ {2}NOTE: 'NEUTRAL / score 0'")
_TRAILER_START = re.compile(
    r"^(?:PAIR OPPORTUNITIES \(|STAT-ARB PAIR BOOK \()")


class _Section:
    __slots__ = ("header", "candidates", "footer")

    def __init__(self, header: str):
        self.header = header
        self.candidates: List[Tuple[str, str]] = []   # (symbol, block)
        self.footer = ""


def _parse_candidate_table(user_msg: str) -> Optional[Dict[str, Any]]:
    """Locate and parse the candidate table. Returns None — "cannot be
    split safely" — on ANY surprise: no region, an unknown top-level
    line, an owned block naming a different symbol, or a parse that
    does not reassemble to the original text byte for byte."""
    m = _REGION_START.search(user_msg)
    if not m:
        return None
    end = user_msg.find(_REGION_END, m.start())
    if end < 0:
        return None
    prefix, region, suffix = (user_msg[:m.start()],
                              user_msg[m.start():end], user_msg[end:])
    sections: List[_Section] = []
    trailer_lines: List[str] = []
    cur: Optional[List[str]] = None      # lines of the open candidate
    cur_sym: Optional[str] = None
    in_trailer = False

    def _close():
        nonlocal cur, cur_sym
        if cur is not None:
            sections[-1].candidates.append(
                (cur_sym, "\n".join(cur).rstrip("\n")))
        cur, cur_sym = None, None

    for line in region.split("\n"):
        if in_trailer:
            trailer_lines.append(line)
            continue
        if _TRAILER_START.match(line):
            _close()
            in_trailer = True
            trailer_lines.append(line)
            continue
        if _SECTION_HEADER.match(line):
            _close()
            sections.append(_Section(line))
            continue
        cm = _CANDIDATE_START.match(line)
        if cm:
            if not sections:
                return None
            _close()
            cur, cur_sym = [line], cm.group(1)
            continue
        if _SECTION_FOOTER.match(line):
            if not sections:
                return None
            _close()
            sections[-1].footer = line
            continue
        if line == "" or line[0] == " ":
            if cur is not None:
                cur.append(line)
            elif line.strip():
                return None          # indented text owned by nothing
            continue
        om = _CANDIDATE_OWNED.match(line)
        if om and cur is not None and (om.group(1) or om.group(2)) == cur_sym:
            cur.append(line)
            continue
        return None                  # unknown top-level line
    _close()

    table = {"prefix": prefix, "sections": sections,
             "trailer": "\n".join(trailer_lines).rstrip("\n"),
             "suffix": suffix}
    if _render_candidate_table(table, None) != user_msg:
        return None
    return table


def _render_candidate_table(table: Dict[str, Any],
                            keep: Optional[set]) -> str:
    """Rebuild the user message showing only the candidates whose
    (section index, candidate index) is in `keep` (None = all). A
    section with no kept candidate is omitted unless it never had any
    (the literal "SHORT CANDIDATES: (none triggered this scan)" line
    is true of the whole scan and stays)."""
    parts: List[str] = []
    for si, sec in enumerate(table["sections"]):
        blocks = [blk for ci, (_sym, blk) in enumerate(sec.candidates)
                  if keep is None or (si, ci) in keep]
        if sec.candidates and not blocks:
            continue
        text = "\n".join([sec.header] + blocks)
        if sec.footer:
            text += "\n" + sec.footer
        parts.append(text)
    if table["trailer"]:
        parts.append(table["trailer"])
    return table["prefix"] + "\n\n".join(parts) + table["suffix"]


def _pack(sizes: List[int], capacity: int) -> Optional[List[List[int]]]:
    """Contiguous, order-preserving partition of candidate indexes into
    the fewest parts that each fit `capacity`, then evened out so one
    part isn't full while the last holds a single candidate. None when
    a single candidate alone exceeds the capacity."""
    if not sizes or max(sizes) > capacity:
        return None

    def _greedy(cap: int) -> List[List[int]]:
        out, part, used = [], [], 0
        for i, sz in enumerate(sizes):
            if part and used + sz > cap:
                out.append(part)
                part, used = [], 0
            part.append(i)
            used += sz
        out.append(part)
        return out

    fewest = _greedy(capacity)
    k = len(fewest)
    lo = max(max(sizes), math.ceil(sum(sizes) / k))
    for cap in range(lo, capacity + 1, max(1, (capacity - lo) // 20 or 1)):
        parts = _greedy(cap)
        if len(parts) == k:
            return parts
    return fewest


def fit_example(ex: Dict[str, Any], counter, max_tokens: int
                ) -> Tuple[List[Dict[str, Any]], str]:
    """Make one cycle example fit the training window.

    Returns (examples, outcome): outcome is "fits" (unchanged),
    "split" (2+ parts, each within the window, each keeping the full
    shared context, its target restricted to the candidates it shows),
    or "dropped" (could not be split safely — never emitted
    truncated). Parts that show no LABELED candidate are not emitted:
    an empty target there would assert "no trade" about candidates
    whose right answer is unknown."""
    if counter.messages(ex["messages"]) <= max_tokens:
        return [ex], "fits"
    system_msg, user_msg, answer = (m["content"] for m in ex["messages"])
    table = _parse_candidate_table(user_msg)
    if table is None:
        return [], "dropped"
    flat = [(si, ci, sym, blk)
            for si, sec in enumerate(table["sections"])
            for ci, (sym, blk) in enumerate(sec.candidates)]
    meta = ex["_meta"]
    labels: Dict[str, str] = meta.get("labels") or {}
    shown = {sym for _si, _ci, sym, _blk in flat}
    if not flat or any(sym not in shown for sym in labels):
        return [], "dropped"     # a labeled symbol has no block to follow
    try:
        trades = json.loads(answer).get("trades") or []
    except (ValueError, AttributeError):
        return [], "dropped"

    shell = [{"role": "system", "content": system_msg},
             {"role": "user",
              "content": _render_candidate_table(table, set())},
             {"role": "assistant", "content": answer}]
    sizes = [counter.text(blk) + 1 for _si, _ci, _sym, blk in flat]
    margin = _PACK_MARGIN_TOKENS
    for _attempt in range(4):
        capacity = max_tokens - margin - counter.messages(shell)
        parts = _pack(sizes, capacity)
        if parts is None:
            return [], "dropped"
        out: List[Dict[str, Any]] = []
        ok = True
        for pi, idxs in enumerate(parts):
            syms = {flat[i][2] for i in idxs}
            if not any(s in labels for s in syms):
                continue
            keep = {(flat[i][0], flat[i][1]) for i in idxs}
            part_trades = [t for t in trades
                           if isinstance(t, dict)
                           and str(t.get("symbol")) in syms]
            msgs = [{"role": "system", "content": system_msg},
                    {"role": "user",
                     "content": _render_candidate_table(table, keep)},
                    {"role": "assistant",
                     "content": json.dumps({"trades": part_trades},
                                           separators=(",", ":"))}]
            if counter.messages(msgs) > max_tokens:
                ok = False
                break
            pmeta = dict(meta)
            for key in ("labels", "returns", "origins", "symbol_ids"):
                if isinstance(meta.get(key), dict):
                    pmeta[key] = {s: v for s, v in meta[key].items()
                                  if s in syms}
            pmeta["ids"] = [i for s in pmeta.get("symbol_ids", {})
                            for i in pmeta["symbol_ids"][s]]
            pmeta["split"] = {"part": pi + 1, "of": len(parts)}
            out.append({"messages": msgs, "_meta": pmeta})
        if ok:
            return (out, "split") if out else ([], "dropped")
        margin *= 2                  # fragments weren't additive enough
    return [], "dropped"


def prune_unlabeled(ex: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Remove from the prompt every candidate that has NO label.

    The target expresses HOLD by omission, so a candidate that is
    shown but unlabeled — its move fell in the ambiguous 2-5% zone, or
    its outcome never resolved cleanly — is silently taught as "no
    trade". The labeling rules exclude those rows precisely so the
    model is taught NOTHING about them; leaving their blocks in the
    prompt taught HOLD anyway (~29% of shown candidates), a systematic
    push toward the blanket-HOLD behaviour batches 2-3 showed. With
    the blocks removed, every target is exactly true of what the
    prompt shows.

    Returns (example, candidates_removed). An example whose table the
    strict parser cannot read is returned unchanged with -1, and the
    caller counts it. Applied to train and val; the exam keeps whole
    prompts (it only ever grades labeled symbols)."""
    system_msg, user_msg, answer = (m["content"] for m in ex["messages"])
    table = _parse_candidate_table(user_msg)
    if table is None:
        return ex, -1
    labels = ex["_meta"].get("labels") or {}
    keep, removed = set(), 0
    for si, sec in enumerate(table["sections"]):
        for ci, (sym, _blk) in enumerate(sec.candidates):
            if sym in labels:
                keep.add((si, ci))
            else:
                removed += 1
    if not removed or not keep:
        return ex, 0
    pruned = dict(ex)
    pruned["messages"] = [
        {"role": "system", "content": system_msg},
        {"role": "user",
         "content": _render_candidate_table(table, keep)},
        {"role": "assistant", "content": answer}]
    return pruned, removed


def fit_split(examples: List[Dict[str, Any]], counter, max_tokens: int
              ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Apply `fit_example` to a whole split. The returned stats are
    the audit trail the manifest carries; `max_tokens_after` proves
    nothing over the window survived (asserted, not assumed)."""
    out: List[Dict[str, Any]] = []
    stats = {"in": len(examples), "fits": 0, "split": 0, "dropped": 0,
             "parts_emitted": 0, "labels_lost_to_drops": 0,
             "max_tokens_after": 0}
    for ex in examples:
        fitted, outcome = fit_example(ex, counter, max_tokens)
        stats[outcome] += 1
        if outcome == "split":
            stats["parts_emitted"] += len(fitted)
        if outcome == "dropped":
            stats["labels_lost_to_drops"] += len(
                ex["_meta"].get("labels") or {})
        out.extend(fitted)
    for ex in out:
        n = counter.messages(ex["messages"])
        assert n <= max_tokens, (
            f"over-length example survived the length pass: {n} > "
            f"{max_tokens} tokens (cycle {ex['_meta'].get('cycle_id')})")
        stats["max_tokens_after"] = max(stats["max_tokens_after"], n)
    stats["out"] = len(out)
    return out, stats


# ---------------------------------------------------------------------------
# Train-split rebalance (docs/28 §4.1)
# ---------------------------------------------------------------------------

def _target_trades(ex: Dict[str, Any]) -> List[Dict[str, Any]]:
    # JSON_OK: the assistant content is always this module's own
    # json.dumps({"trades": [...]}) (build_cycle_example / fit_example).
    return json.loads(ex["messages"][-1]["content"]).get("trades") or []


def _has_direction(ex: Dict[str, Any], actions: frozenset) -> bool:
    return any(lbl in actions
               for lbl in (ex["_meta"].get("labels") or {}).values())


def split_stats(examples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The shape of a split, as the loss sees it. HOLD is an omission,
    so the quantities that matter are how often the target is EMPTY
    and how many trades it lists — reported beside the label and
    label-origin censuses."""
    n = len(examples)
    n_trades = [len(_target_trades(e)) for e in examples]
    origins: Dict[str, int] = {}
    directional = missed = 0
    for e in examples:
        labels = e["_meta"].get("labels") or {}
        for sym, org in (e["_meta"].get("origins") or {}).items():
            origins[org] = origins.get(org, 0) + 1
            if labels.get(sym) in _BULLISH_ACTIONS | _BEARISH_ACTIONS:
                directional += 1
                missed += org == "missed_move"
    empty = sum(1 for t in n_trades if t == 0)
    return {
        "examples": n,
        "empty_target_fraction": round(empty / n, 4) if n else None,
        "mean_trades_per_target": round(sum(n_trades) / n, 3) if n else None,
        "examples_with_buy": sum(
            _has_direction(e, _BULLISH_ACTIONS) for e in examples),
        "examples_with_short": sum(
            _has_direction(e, _BEARISH_ACTIONS) for e in examples),
        "label_distribution": _label_dist(examples),
        "origin_distribution": origins,
        "missed_move_share_of_directional": (
            round(missed / directional, 4) if directional else None),
    }


def rebalance_train(
    examples: List[Dict[str, Any]],
    *,
    max_empty_fraction: float = 0.10,
    direction_ratio: float = 1.25,
    missed_move_cap: Optional[float] = None,
    seed: int = 1729,
) -> List[Dict[str, Any]]:
    """Reshape the TRAIN split by dropping whole examples — never by
    splitting a cycle or editing a target, which must stay the true
    corrected action set for its prompt.

    Order: missed-move cap (off by default) → direction balance →
    empty-target cap. The empty cap runs LAST because the two earlier
    steps remove action-bearing examples, which would push the empty
    fraction back above the cap if it had been applied first."""
    rng = random.Random(seed)
    pool = list(examples)

    if missed_move_cap is not None:
        def _dir_counts(e):
            labels = e["_meta"].get("labels") or {}
            orgs = e["_meta"].get("origins") or {}
            d = [s for s, l in labels.items()
                 if l in _BULLISH_ACTIONS | _BEARISH_ACTIONS]
            return len(d), sum(orgs.get(s) == "missed_move" for s in d)
        counts = [_dir_counts(e) for e in pool]
        total = sum(c[0] for c in counts)
        missed = sum(c[1] for c in counts)
        # Only examples whose EVERY directional label is a missed move
        # are droppable — dropping a mixed one would discard real
        # entries along with the noise.
        droppable = [i for i, (d, mm) in enumerate(counts) if d and d == mm]
        rng.shuffle(droppable)
        dropped = set()
        for i in droppable:
            if total == 0 or missed / total <= missed_move_cap:
                break
            dropped.add(i)
            total -= counts[i][0]
            missed -= counts[i][1]
        pool = [e for i, e in enumerate(pool) if i not in dropped]

    n_buy = sum(_has_direction(e, _BULLISH_ACTIONS) for e in pool)
    n_short = sum(_has_direction(e, _BEARISH_ACTIONS) for e in pool)
    if n_buy and n_short and max(n_buy, n_short) > direction_ratio * min(
            n_buy, n_short):
        major, minor = ((_BULLISH_ACTIONS, _BEARISH_ACTIONS)
                        if n_buy > n_short
                        else (_BEARISH_ACTIONS, _BULLISH_ACTIONS))
        excess = math.ceil(max(n_buy, n_short)
                           - direction_ratio * min(n_buy, n_short))
        single = [i for i, e in enumerate(pool)
                  if _has_direction(e, major)
                  and not _has_direction(e, minor)]
        rng.shuffle(single)
        dropped = set(single[:excess])
        pool = [e for i, e in enumerate(pool) if i not in dropped]

    empties = [e for e in pool if not _target_trades(e)]
    nonempty = [e for e in pool if _target_trades(e)]
    if max_empty_fraction >= 1.0:
        allowed = len(empties)
    else:
        allowed = int(max_empty_fraction * len(nonempty)
                      / (1.0 - max_empty_fraction))
    if len(empties) > allowed:
        # Prefer empties that contain a LOSING ENTRY ("this looked
        # good and wasn't") over ones made only of flat easy negatives.
        def _has_lost(e):
            return "lost_entry" in (e["_meta"].get("origins") or {}).values()
        lost = [e for e in empties if _has_lost(e)]
        flat = [e for e in empties if not _has_lost(e)]
        rng.shuffle(lost)
        rng.shuffle(flat)
        empties = (lost + flat)[:allowed]
    pool = nonempty + empties
    rng.shuffle(pool)
    return pool


def _fill_from_cycle(row: Dict[str, Any], prompt: Optional[str],
                     raw: Optional[str]) -> Dict[str, Any]:
    """2026-08-26 — cycle-join enrichment. Since 2026-07-02 the prompt
    and raw response live ONCE on the ai_cycles row (per-prediction
    storage duplicated the same bytes onto every candidate of the
    cycle, 6.15x bloat) and the prediction row carries only cycle_id.
    This builder was written against the pre-07-02 shape and rejected
    100% of post-move rows as "stub" (the zero-example corpus of
    2026-08-26). Row-level values, when present (pre-07-02 rows),
    always win — enrichment only fills gaps."""
    if not row.get("prompt_text") and prompt:
        row["prompt_text"] = prompt
    if not row.get("raw_response_json") and raw:
        row["raw_response_json"] = raw
    return row


def _iter_live_rows(profile_db: str) -> Iterable[Dict[str, Any]]:
    """Yield resolved ai_predictions rows from a profile journal,
    joined to their cycle's prompt/raw response (see _fill_from_cycle)."""
    import sqlite3
    try:
        with closing(sqlite3.connect(profile_db)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT p.*, c.prompt_text AS _cycle_prompt, "
                    "       c.raw_response_json AS _cycle_raw "
                    "FROM ai_predictions p "
                    "LEFT JOIN ai_cycles c ON c.cycle_id = p.cycle_id "
                    "WHERE p.status = 'resolved'"
                ).fetchall()
            except sqlite3.OperationalError:
                # Pre-07-02 journal without ai_cycles — row-level
                # prompt storage; no join needed.
                rows = conn.execute(
                    "SELECT * FROM ai_predictions WHERE status = 'resolved'"
                ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("dataset_builder: read %s failed: %s",
                       profile_db, exc)
        return
    for r in rows:
        d = dict(r)
        yield _fill_from_cycle(d, d.pop("_cycle_prompt", None),
                               d.pop("_cycle_raw", None))


def _profile_key(path: str) -> str:
    """Stable per-profile dedup namespace from a live DB path
    (quantopsai_profile_<pid>.db) or an archive dump directory
    (…/predictions_archive/<pid>/<stamp>). The same profile's live
    journal and archived dumps share one namespace, so live/archive
    copies of a row still dedup — while different profiles' identical
    autoincrement ids never collide (the 2026-08-27 swallowed-rows
    bug)."""
    p = Path(path)
    m = re.search(r"quantopsai_profile_(\d+)", p.name)
    if m:
        return m.group(1)
    return p.parent.name or p.name


def _iter_archive_rows_for_dump(dump: Path) -> Iterable[Dict[str, Any]]:
    """Yield rows from ONE archive dump directory, each joined to its
    cycle's prompt/raw response from the dump's own cycles.jsonl (see
    _fill_from_cycle — post-2026-07-02 rows carry only cycle_id)."""
    for jsonl in [dump / "predictions.jsonl"]:
        if not jsonl.exists():
            continue
        cyc: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        cpath = jsonl.parent / "cycles.jsonl"
        if cpath.exists():
            try:
                with open(cpath) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            c = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        cid = c.get("cycle_id")
                        if cid:
                            cyc[cid] = (c.get("prompt_text"),
                                        c.get("raw_response_json"))
            except OSError as exc:
                logger.warning(
                    "dataset_builder: cycles read %s failed: %s — this "
                    "dump's post-07-02 rows will lack prompts and be "
                    "filtered", cpath, exc)
        try:
            with open(jsonl) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    prompt, raw = cyc.get(row.get("cycle_id"),
                                          (None, None))
                    yield _fill_from_cycle(row, prompt, raw)
        except OSError as exc:
            logger.warning("dataset_builder: archive read %s failed: %s",
                           jsonl, exc)


def _iter_archive_rows(archive_root: str) -> Iterable[Dict[str, Any]]:
    """All dumps under predictions_archive/*/* (kept for callers that
    don't need per-profile dedup namespaces; build_dataset iterates
    per dump so the (profile, id) key can be applied)."""
    root = Path(archive_root)
    if not root.exists():
        return
    for jsonl in root.glob("*/*/predictions.jsonl"):
        yield from _iter_archive_rows_for_dump(jsonl.parent)


def build_dataset(
    profile_dbs: List[str],
    out_dir: str,
    *,
    archive_root: Optional[str] = "backups/predictions_archive",
    allow_short: bool = True,
    eval_holdout: int = 200,
    eval_days: Optional[int] = None,
    prune_unlabeled_candidates: bool = True,
    val_fraction: float = 0.10,
    seed: int = 1729,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    token_counter=None,
    rebalance: bool = True,
    max_empty_fraction: float = 0.10,
    direction_ratio: float = 1.25,
    missed_move_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """Build train/val/eval chat JSONL files from live + archived
    predictions across the given profile journals.

    Dedups by (profile, prediction id) across live + archive. The
    split is ordered in TIME — train, then val, then the exam — and
    train is purged of any example whose label resolved inside what
    follows it (see the comment at the split). The exam is
    `eval_holdout` cycles: the most recent ones, or, with `eval_days`,
    sampled evenly across the last that-many decision dates.

    Then, in this order (docs/28):
      1. LENGTH PASS on every split — over-length examples are split
         along the candidate table or dropped and counted
         (`fit_split`). It runs AFTER the cycle-level split so a
         cycle's parts can never straddle train/val/eval (they share
         most of their prompt; a part leaking into val or the exam
         would contaminate it).
      2. REBALANCE on the TRAIN split only (`rebalance_train`). Val
         and eval keep the natural distribution — they measure the
         real task, and a rebalanced exam would flatter the model.

    `token_counter` defaults to the tokenizer-free estimate; the Mac
    driver passes an exact counter. Returns a manifest dict that
    reports every stage before and after — a rebalance or a length
    pass that cannot be audited from the manifest is not done.
    """
    counter = token_counter or CharEstimateCounter()
    seen_ids = set()
    # (cycle_key, prompt-identity) → [(row, label, ret), ...]. The
    # prompt hash sub-key is a safety guard: two profiles can mint the
    # same cycle_id string, but their prompts differ — rows only group
    # when they truly answered the SAME prompt.
    groups: Dict[Tuple[Any, int], List[Tuple[Dict[str, Any], str, float]]] = {}
    labeled_rows = 0
    # Labeled decisions per source profile — a profile that silently
    # contributes nothing must be visible in the manifest.
    by_source: Dict[str, int] = {}

    def _ingest(rows: Iterable[Dict[str, Any]], source_key: str = ""):
        nonlocal labeled_rows
        by_source.setdefault(source_key, 0)
        for row in rows:
            rid = row.get("id")
            # 2026-08-27 — dedup by (PROFILE, id), never bare id: every
            # profile journal autoincrements from 1, so bare-id dedup
            # silently swallowed later profiles' rows as "duplicates"
            # across the 22 archive dumps (it ate all 14 option
            # premium-winners and an unknown slice of the stock corpus).
            # The key still collapses the same profile's live row with
            # its archived copy — the overlap dedup was built for.
            key = ((source_key, rid) if rid is not None
                   else ("", id(row)))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            labeled = _label_and_return(row, allow_short=allow_short)
            if labeled is None:
                continue
            label, ret = labeled
            labeled_rows += 1
            by_source[source_key] += 1
            cycle_key = row.get("cycle_id") or f"__row_{key}"
            pkey = hash(row.get("prompt_text") or "")
            groups.setdefault((cycle_key, pkey), []).append(
                (row, label, ret))

    for db in profile_dbs:
        _ingest(_iter_live_rows(db), source_key=_profile_key(db))
    if archive_root:
        for dump in sorted(Path(archive_root).glob("*/*")):
            if not (dump / "predictions.jsonl").exists():
                continue
            _ingest(_iter_archive_rows_for_dump(dump),
                    source_key=_profile_key(str(dump)))

    # 2026-08-27 — CYCLE-GROUPED examples (production output shape):
    # one example per cycle; target = corrected actions for every
    # labeled candidate, HOLDs by omission. See build_cycle_example.
    examples: List[Dict[str, Any]] = []
    for group in groups.values():
        ex = build_cycle_example(group)
        if ex is not None:
            examples.append(ex)

    # Most-recent-first by prediction timestamp for the eval holdout.
    examples.sort(
        key=lambda e: e["_meta"].get("timestamp") or "",
        reverse=True,
    )
    # TIME-ORDERED split with a purge:   train │ val │ eval  (newest).
    #
    # A label is a FORWARD return, known only at `resolved_at`, days
    # after the decision. Twelve replicate profiles see largely the
    # same symbols at the same moments, so a training example decided
    # the day before the exam window carries a label that was earned
    # INSIDE it ("NVDA fell that week"). Training on it lets the
    # adapter score on the exam from memorised symbol-and-week
    # outcomes the untrained base never saw — a leak that flatters
    # exactly the comparison the promotion bar rests on. So:
    #   - val is the block just before the exam (not a random sample,
    #     which the same leak would turn into a memorisation meter —
    #     and checkpoints are ranked on it);
    #   - any TRAIN example whose label resolved at or after the first
    #     decision of what follows it (val, else the exam period) is
    #     purged. Val itself is not purged against the exam: it is
    #     never trained on, and outcomes take 5-8 days to resolve, so
    #     purging it would empty it (it did, on the first build).
    #
    # The exam PERIOD (`eval_days`): because outcomes take days to
    # resolve, "the N most recent resolved cycles" is a single trading
    # day — one market regime, seen twelve times over. With eval_days
    # set, the exam is `eval_holdout` cycles sampled evenly across the
    # last `eval_days` decision dates; the rest of that period is
    # simply unused (it cannot be trained on).
    def _key(value: Any) -> str:
        return str(value or "").replace("T", " ")[:19]

    rng = random.Random(seed)
    exam_period: Dict[str, int] = {}
    if eval_days and eval_holdout:
        dates = sorted({_key(e["_meta"].get("timestamp"))[:10]
                        for e in examples}, reverse=True)[:eval_days]
        by_day = {d: [e for e in examples
                      if _key(e["_meta"].get("timestamp"))[:10] == d]
                  for d in dates}
        exam_period = {d: len(v) for d, v in sorted(by_day.items())}
        eval_set = []
        share, extra = divmod(eval_holdout, max(1, len(dates)))
        for i, d in enumerate(sorted(by_day)):
            pool = list(by_day[d])
            rng.shuffle(pool)
            eval_set += pool[:share + (1 if i < extra else 0)]
        period_start = min(dates) if dates else ""
        rest = [e for e in examples
                if _key(e["_meta"].get("timestamp"))[:10] < period_start]
    else:
        eval_set = examples[:eval_holdout]
        rest = examples[eval_holdout:]
        period_start = min((_key(e["_meta"].get("timestamp"))
                            for e in eval_set), default="")
    eval_set.sort(key=lambda e: _key(e["_meta"].get("timestamp")),
                  reverse=True)
    n_val = int(len(rest) * val_fraction)
    val_set = rest[:n_val]
    train_set = rest[n_val:]

    boundary = min((_key(e["_meta"].get("timestamp")) for e in val_set),
                   default=period_start)
    purged_train = 0
    if boundary:
        kept = [e for e in train_set
                if _key(e["_meta"].get("resolved_at")) < boundary]
        purged_train = len(train_set) - len(kept)
        train_set = kept
    rng.shuffle(train_set)

    # 0. Prune unlabeled candidates from TRAIN and VAL prompts, so a
    #    target never asserts "no trade" about a candidate whose right
    #    answer is unknown (see prune_unlabeled). Before the length
    #    pass: a pruned prompt is shorter and splits less.
    prune_stats: Dict[str, Dict[str, int]] = {}
    if prune_unlabeled_candidates:
        for name, block in (("train", train_set), ("val", val_set)):
            st = {"examples_pruned": 0, "candidates_removed": 0,
                  "unparseable_left_whole": 0}
            for i, ex in enumerate(block):
                pruned, removed = prune_unlabeled(ex)
                if removed < 0:
                    st["unparseable_left_whole"] += 1
                elif removed:
                    block[i] = pruned
                    st["examples_pruned"] += 1
                    st["candidates_removed"] += removed
            prune_stats[name] = st

    # 1. Length pass — every split, after the cycle-level assignment.
    length_stats: Dict[str, Dict[str, int]] = {}
    train_set, length_stats["train"] = fit_split(train_set, counter,
                                                 max_tokens)
    val_set, length_stats["val"] = fit_split(val_set, counter, max_tokens)
    eval_set, length_stats["eval"] = fit_split(eval_set, counter,
                                               max_tokens)
    for name, st in length_stats.items():
        if st["dropped"]:
            logger.warning(
                "dataset_builder: %s split dropped %d over-length "
                "example(s) that could not be split safely (%d labeled "
                "decisions lost) — see manifest['length']",
                name, st["dropped"], st["labels_lost_to_drops"])

    # 2. Rebalance — TRAIN only.
    train_before = split_stats(train_set)
    if rebalance:
        train_set = rebalance_train(
            train_set, max_empty_fraction=max_empty_fraction,
            direction_ratio=direction_ratio,
            missed_move_cap=missed_move_cap, seed=seed)
    # (The look-ahead guard ran on every row inside
    # build_cycle_example — before any example existed — so every row
    # that survives the two passes above has already been proven.)

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
        "labeled_rows": labeled_rows,
        "train": len(train_set),
        "val": len(val_set),
        "eval": len(eval_set),
        "paths": paths,
        "label_distribution": _label_dist(examples),
        "labeled_rows_by_source": dict(sorted(by_source.items())),
        # Time-ordered split + purge (see the comment at the split).
        "split": {
            "order": "time: train | val | eval (newest)",
            "eval_first_decision": min(
                (_key(e["_meta"].get("timestamp")) for e in eval_set),
                default=None),
            "val_first_decision": min(
                (_key(e["_meta"].get("timestamp")) for e in val_set),
                default=None),
            "train_last_decision": max(
                (_key(e["_meta"].get("timestamp")) for e in train_set),
                default=None),
            "train_cycles_purged": purged_train,
            # {decision date: cycles available that day}; the exam is
            # sampled evenly across these (empty = most-recent-N mode).
            "exam_period_cycles_by_day": exam_period,
        },
        # Unlabeled candidates removed from train/val prompts (the exam
        # keeps whole prompts). {} when pruning is off.
        "prune": prune_stats,
        "length": {
            "method": counter.method,
            "max_tokens": max_tokens,
            **length_stats,
        },
        "rebalance": {
            "applied": bool(rebalance),
            "max_empty_fraction": max_empty_fraction,
            "direction_ratio": direction_ratio,
            "missed_move_cap": missed_move_cap,
            "seed": seed,
            "train_before": train_before,
            "train_after": split_stats(train_set),
        },
        # Natural distribution — never rebalanced; this is the task.
        "val_stats": split_stats(val_set),
        "eval_stats": split_stats(eval_set),
    }
    logger.info("dataset_builder: built corpus %s", manifest)
    return manifest


def _label_dist(examples: List[Dict[str, Any]]) -> Dict[str, int]:
    dist: Dict[str, int] = {}
    for e in examples:
        labels = e["_meta"].get("labels")
        if isinstance(labels, dict):
            for lbl in labels.values():
                dist[lbl] = dist.get(lbl, 0) + 1
        else:
            lbl = e["_meta"].get("label", "?")
            dist[lbl] = dist.get(lbl, 0) + 1
    return dist
