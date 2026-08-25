# Calculation Verification Register

**Purpose.** A complete, human-auditable accounting of every calculation
displayed on (or used by) the QuantOpsAI web application: what is
computed, exactly how, why it is done that way, and whether it has been
verified correct against the implementation and live data.

**This folder is deliberately NOT served by the in-app Documents tab**
(the docs viewer lists only flat files inside `docs/`; this folder sits
outside it by construction). It exists for auditors and the operator.

## How each item is documented

Every register entry carries five fields:

| Field | Meaning |
|---|---|
| **Item** | The label exactly as the user sees it on the page |
| **Computed as** | The implemented formula, in plain terms, with the source location (`file:function`) |
| **Why this way** | The design rationale — usually two or three sentences, including incident history where the formula was shaped by one |
| **Inputs** | The tables/columns/services the number derives from |
| **Verification** | `VERIFIED` (code read + formula confirmed + spot-checked against live or fixture data), `VERIFIED (code)` (formula confirmed from code; no independent numeric spot-check possible), or `SUSPECT` with the concern stated |

**SUSPECT entries are the point of this register.** Anything marked
SUSPECT is collected in [SUSPECTS.md](SUSPECTS.md) for operator review;
an item stays suspect until it is either fixed (with the fix noted) or
explicitly accepted with rationale.

## Coverage tracker

An audit is only as good as its coverage accounting. Every page ships
its own file; a page is DONE only when every displayed number on it has
a register entry.

| Page | File | Status |
|---|---|---|
| /performance | [performance.md](performance.md) | **DONE** |
| /dashboard | [dashboard.md](dashboard.md) | **DONE** |
| /trades | [trades.md](trades.md) | **DONE** |
| /ai (+brain/strategy/awareness/operations) | [ai.md](ai.md) | **DONE** |
| /ai-performance | [ai_performance.md](ai_performance.md) | **DONE** |
| /shadow | [shadow.md](shadow.md) | **DONE** |
| /issues | [issues.md](issues.md) | **DONE** |
| /backtest (+history) | [backtest.md](backtest.md) | **DONE** |
| /universe popup | [universe.md](universe.md) | **DONE** |
| /admin | [admin.md](admin.md) | **DONE** |
| Daily email digest | [email_digest.md](email_digest.md) | **DONE** |
| Virtual benchmarks (comparative chart series) | [benchmarks.md](benchmarks.md) | **DONE** |
| /learning (Learning Scoreboard) | [learning.md](learning.md) | **DONE** |

## Conventions the whole system shares

These cross-cutting rules are documented once here and referenced by
the per-page files:

1. **Fill-true pricing.** Anywhere a trade's execution price matters,
   the expression is `COALESCE(NULLIF(fill_price,0), price)` — the
   broker's actual fill when known, the decision price only as
   fallback. Two implementations of "what did this cost" is how books
   and metrics drift apart (2026-08-13 incident: a `price` column
   carrying $2.13 against a $328.03 fill).
2. **Option contract multiplier.** Every option notional is
   `premium × qty × 100`. Omitting the multiplier understates basis
   100× and produced the −4360% CVaR (2026-08-12).
3. **Dead-status exclusion, bounded by the FILL-TRUTH INVARIANT
   (2026-08-25).** Money and position lenses exclude rows in
   `('canceled','expired','rejected','done_for_day',
   'pending_protective','auto_reconciled_phantom_close')` — statuses
   that mean "no money moved via this row" — plus
   `'auto_closed_external'` **only when the row bears no fill**. A row
   whose order actually filled (`fill_price` stamped) NEVER leaves the
   cash algebra or the realized FIFO, no matter what status a
   reconcile path later writes on it: an externally-closed position
   leaves the POSITION lens, but its real money stays booked. (The
   old wholesale exclusion assumed "cash booked by the activities
   pass," which books broker events, never ordinary fills — that
   assumption was the five-premium hole of 2026-08-25: mislabeled own
   round-trips drifted equity by +$185…+$4,550 and cash parity by the
   exact premiums.) The exit-side and entry-side sets are aligned by
   structural test; the invariant is pinned by
   `tests/test_option_own_close_not_external_2026_08_25.py`.
4. **Degraded-book contract.** If a profile's journal is unreadable,
   money displays must render "unavailable" — never a fabricated $0 or
   the initial-capital fallback presented as live.
5. **Never 0% for unmeasured.** Rates derived from an empty sample
   render as absent ("—"), never as zero, which would read as
   "measured and found zero."
6. **Vendor-fair structured output (2026-08-23).** Every AI call that
   needs a shaped answer passes a JSON schema through `call_ai`, and
   every vendor is held to it through its own native mechanism
   (Anthropic forced tool_use, OpenAI strict json_schema, Gemini
   response_json_schema); shadow arms receive the same schema. Any
   number derived from model verdicts — agreement, win rates, edges —
   therefore compares models, never parsers. Pinned by
   `tests/test_structured_output_vendor_fair_2026_08_23.py`.
