"""Phase 2 of docs/17 — in-context retrieval (RAG) over resolved
post-mortems.

The LLM's weights don't update from outcomes. The compensating
mechanism is: on every new decision, retrieve the AI's most-similar
past *resolved* cases from THIS profile's own history and inject
them into the prompt as concrete cases-to-reason-from. The system
gains experience without retraining the model.

Design:
  - Case file = a structured per-prediction text built from existing
    `ai_predictions` columns (symbol, signal, regime, indicators
    from features_json, outcome, return). No new schema needed.
  - Embedding = TF-IDF over the case-file corpus (uses already-
    installed scikit-learn; sentence-transformers would be heavier
    and not noticeably better on highly-structured text).
  - Retrieval = cosine similarity, top-N above a minimum threshold
    so junk matches don't crowd out useful ones.
  - Prompt injection = a "SIMILAR PAST CASES" block per candidate
    symbol, surfaced by `ai_analyst._build_batch_prompt`.

Per `feedback_self_tuner_must_drift_toward_trading`, retrieval
returns BOTH wins and losses — the LLM needs both base rates to
calibrate. Filtering to only "warnings" (losses) would bias the
system away from action.

Per `feedback_no_silent_failures`, every error in this module is
either logged or returned as an empty result (no swallowed
exceptions). The retrieval path is fail-soft: on any failure the
candidate's prompt simply doesn't get an injected block — the
existing prompt construction still works, the AI just doesn't get
the extra context.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Knobs — exposed for tests; production rarely tunes.
DEFAULT_TOP_N = 3
DEFAULT_MIN_SIMILARITY = 0.15      # cosine threshold
DEFAULT_MAX_CORPUS_SIZE = 2000     # rolling-window cap on cases vectorized per call


# Features we surface in the case-file text. Keys absent from a
# row's features_json are skipped silently — old predictions
# pre-date some features. Numeric features are bucketed into bands
# so TF-IDF treats e.g. "rsi_70_80" as a discrete token (matching
# improves; otherwise the raw float would be a unique token per
# row and contribute nothing to similarity).
_BUCKETED_FEATURES = {
    "rsi": [(0, 30), (30, 50), (50, 70), (70, 80), (80, 100)],
    "momentum_5d": [(-99, 0), (0, 2), (2, 5), (5, 10), (10, 99)],
    "momentum_20d": [(-99, 0), (0, 5), (5, 10), (10, 20), (20, 99)],
    "volume_ratio": [(0, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 5.0), (5.0, 99)],
    "gap_pct": [(-99, -3), (-3, -1), (-1, 1), (1, 3), (3, 99)],
    "atr_pct": [(0, 1), (1, 2), (2, 4), (4, 7), (7, 99)],
}


def _bucket(value: float, bands: List[Tuple[float, float]]) -> str:
    """Return a stable band label like 'rsi_70_80' for tokenization."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    for lo, hi in bands:
        if lo <= v < hi:
            # Strip trailing .0 for int-shaped bands to keep tokens tidy
            lo_s = str(int(lo)) if lo == int(lo) else str(lo)
            hi_s = str(int(hi)) if hi == int(hi) else str(hi)
            return f"{lo_s}_{hi_s}"
    return ""


def build_case_file_text(prediction: Dict[str, Any],
                          *, include_outcome: bool = True) -> str:
    """Render a `ai_predictions` row as a structured token stream
    suitable for TF-IDF. When `include_outcome=False` (used for the
    NEW candidate at retrieval time), the outcome/return/days_held
    tokens are omitted — otherwise they'd be missing in the query
    text and present in every corpus document, breaking similarity.
    """
    tokens: List[str] = []
    sym = (prediction.get("symbol") or "").upper()
    if sym:
        tokens.append(f"symbol_{sym}")
    sig = (prediction.get("predicted_signal") or "").upper()
    if sig:
        tokens.append(f"signal_{sig}")
    regime = prediction.get("regime_at_prediction")
    if regime:
        tokens.append(f"regime_{regime}")
    strategy = prediction.get("strategy_type")
    if strategy:
        tokens.append(f"strategy_{strategy}")

    # Confidence bucket — coarse so 70 and 72 are the same token.
    conf = prediction.get("confidence")
    if conf is not None:
        try:
            b = int(float(conf) // 10) * 10
            tokens.append(f"confidence_{b}")
        except (TypeError, ValueError):
            pass

    # Indicators from features_json
    features = prediction.get("features_json")
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except (TypeError, ValueError):
            features = None
    if isinstance(features, dict):
        for key, bands in _BUCKETED_FEATURES.items():
            if key not in features:
                continue
            band = _bucket(features[key], bands)
            if band:
                tokens.append(f"{key}_{band}")

    if include_outcome:
        outcome = (prediction.get("actual_outcome") or "").lower()
        if outcome:
            tokens.append(f"outcome_{outcome}")
        ret = prediction.get("actual_return_pct")
        if ret is not None:
            try:
                ret_f = float(ret)
                # Bucket return into 5 bands
                if ret_f <= -5:
                    rb = "ret_below_neg5"
                elif ret_f <= -1:
                    rb = "ret_neg5_to_neg1"
                elif ret_f < 1:
                    rb = "ret_neg1_to_1"
                elif ret_f < 5:
                    rb = "ret_1_to_5"
                else:
                    rb = "ret_above_5"
                tokens.append(rb)
            except (TypeError, ValueError):
                pass

    return " ".join(tokens)


def _fetch_resolved_cases(profile_db_path: str,
                           max_corpus_size: int) -> List[Dict[str, Any]]:
    """Pull the most-recent N resolved predictions, newest first.
    Returns dict rows for `build_case_file_text` consumption.

    The rolling-window cap keeps the TF-IDF fit fast even on long
    histories — old cases are still informative but newer ones
    weigh more in similarity rankings because the corpus is fresher.
    """
    if not profile_db_path or not os.path.exists(profile_db_path):
        return []
    try:
        with closing(sqlite3.connect(profile_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, symbol, predicted_signal, "
                "  confidence, regime_at_prediction, strategy_type, "
                "  features_json, actual_outcome, actual_return_pct, "
                "  days_held, resolved_at "
                "FROM ai_predictions "
                "WHERE status = 'resolved' "
                "  AND actual_outcome IN ('win', 'loss') "
                "ORDER BY resolved_at DESC "
                "LIMIT ?",
                (int(max_corpus_size),),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        logger.warning(
            "case_file_rag: corpus fetch failed for %s: %s",
            profile_db_path, exc,
        )
        return []


def retrieve_similar(
    profile_db_path: str,
    candidate: Dict[str, Any],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    max_corpus_size: int = DEFAULT_MAX_CORPUS_SIZE,
    cases_override: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return the top-N most-similar resolved cases to `candidate`,
    each annotated with `similarity` (cosine 0.0–1.0) and a rendered
    one-line summary.

    Filters out matches below `min_similarity` so the LLM doesn't
    see noise. Same-symbol matches are NOT specially boosted —
    TF-IDF already weights the symbol token; a same-symbol case
    naturally dominates if its other context matches too.

    `cases_override` is a test seam — pass a prebuilt corpus to
    skip the DB fetch.
    """
    cases = (cases_override
             if cases_override is not None
             else _fetch_resolved_cases(profile_db_path, max_corpus_size))
    if not cases:
        return []

    # Lazy import: sklearn isn't free to import.
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError as exc:
        logger.warning(
            "case_file_rag: sklearn unavailable, retrieval disabled: %s",
            exc,
        )
        return []

    corpus_texts = [build_case_file_text(c) for c in cases]
    candidate_text = build_case_file_text(candidate, include_outcome=False)

    # Drop empty docs — vectorizing nothing crashes the fit. An
    # empty candidate text is the test-safe early exit.
    nonempty_indices = [i for i, t in enumerate(corpus_texts) if t.strip()]
    if not nonempty_indices or not candidate_text.strip():
        return []
    docs = [corpus_texts[i] for i in nonempty_indices] + [candidate_text]

    try:
        # token_pattern matches our underscore-joined tokens —
        # default tokenization splits on non-word chars and would
        # break e.g. "rsi_70_80" into three useless tokens.
        vec = TfidfVectorizer(token_pattern=r"\S+", lowercase=True)
        matrix = vec.fit_transform(docs)
    except ValueError as exc:
        # Empty vocabulary (all-blank corpus) — fail-soft.
        logger.debug("case_file_rag: vectorizer fit failed: %s", exc)
        return []

    candidate_vec = matrix[-1]
    corpus_matrix = matrix[:-1]
    sims = cosine_similarity(candidate_vec, corpus_matrix).ravel()

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for local_idx, sim in enumerate(sims):
        if sim < min_similarity:
            continue
        case = cases[nonempty_indices[local_idx]]
        case["similarity"] = float(sim)
        case["case_file_text"] = corpus_texts[nonempty_indices[local_idx]]
        ranked.append((float(sim), case))

    ranked.sort(key=lambda x: -x[0])
    return [c for _, c in ranked[:top_n]]


def format_cases_for_prompt(cases: List[Dict[str, Any]]) -> str:
    """Render the retrieved cases as a compact bulleted block for
    the AI prompt. Output is plain text — the prompt builder
    concatenates this block directly.

    Empty input returns an empty string so callers can splice
    unconditionally.
    """
    if not cases:
        return ""
    lines = []
    for i, c in enumerate(cases, 1):
        date = (c.get("resolved_at") or c.get("timestamp") or "")[:10]
        sym = c.get("symbol", "?")
        sig = c.get("predicted_signal", "?")
        regime = c.get("regime_at_prediction") or "?"
        outcome = (c.get("actual_outcome") or "?").upper()
        ret = c.get("actual_return_pct")
        days = c.get("days_held")
        sim = c.get("similarity", 0.0)

        ret_str = f"{ret:+.1f}%" if isinstance(ret, (int, float)) else "?"
        days_str = f"{days}d" if days is not None else "?d"

        # First line: outcome headline
        lines.append(
            f"  {i}. [{date}] {sig} {sym} in {regime} → {outcome} "
            f"({ret_str} in {days_str}, sim={sim:.2f})"
        )

        # Optional second line: indicators that drove the match
        feats = c.get("features_json")
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except (TypeError, ValueError):
                feats = None
        if isinstance(feats, dict):
            kv_bits = []
            for k in ("rsi", "momentum_5d", "volume_ratio", "gap_pct", "atr_pct"):
                if k in feats and feats[k] is not None:
                    try:
                        kv_bits.append(f"{k}={float(feats[k]):.1f}")
                    except (TypeError, ValueError):
                        pass
            if kv_bits:
                lines.append("     " + ", ".join(kv_bits))
    return "\n".join(lines)


def build_prompt_block(profile_db_path: str,
                       candidate: Dict[str, Any],
                       *,
                       top_n: int = DEFAULT_TOP_N,
                       min_similarity: float = DEFAULT_MIN_SIMILARITY,
                       ) -> str:
    """End-to-end: retrieve + format. Returns the complete prompt
    block (with header) or an empty string when no useful matches.

    The caller can splice the return value into the prompt without
    a conditional — empty string means "nothing to add."
    """
    cases = retrieve_similar(
        profile_db_path, candidate,
        top_n=top_n, min_similarity=min_similarity,
    )
    if not cases:
        return ""
    sym = candidate.get("symbol", "this candidate")
    header = f"\nSIMILAR PAST CASES (your own resolved trades) FOR {sym}:\n"
    return header + format_cases_for_prompt(cases)
