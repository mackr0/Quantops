// Shared price/number formatters for QuantOpsAI dashboard JS.
//
// Issue 13 (2026-05-10): templates had at least 4 distinct inline
// `function fmt(n)` implementations with inconsistent conventions
// (some with thousands commas, some not; some with $ sign, some not;
// some with 0 decimals, some with 2). When the AI dashboard's cost
// widget shipped a different format than the dashboard's pending-orders
// widget, the user got mixed conventions across the same page.
//
// All shared formatters live here. Inline `function fmt*` declarations
// in <script> blocks are blocked by `tests/test_no_inline_js_formatters.py`.
//
// API choice: namespace as `QF` (QuantOps Format) so calls read clearly:
//   QF.dollars2(n)        → "$12,345.67"
//   QF.dollars0(n)        → "$12,345"
//   QF.signedDollars0(n)  → "+$12,345" / "-$5,432"
//   QF.intCommas(n)       → "12,345"
//   QF.signedPct(n, dp)   → "+1.23%" / "-0.45%"
//   QF.percent(n, dp)     → "12.3%"
//
// Server-side rendering is still preferred when the formatted string
// can be precomputed in the API response (see *_label, *_friendly
// patterns elsewhere). These JS helpers exist for values that JS
// derives at render time (subtractions, ratios, etc.).

(function (root) {
    'use strict';

    function _toNum(n) {
        if (n == null) return null;
        var v = Number(n);
        return isFinite(v) ? v : null;
    }

    var QF = {};

    // "$12,345.67" — currency with comma thousands and exactly 2 decimals.
    // Use case: prices, equity (when fractional cents matter).
    QF.dollars2 = function (n) {
        var v = _toNum(n);
        if (v === null) return '';
        return '$' + v.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    };

    // "$12,345" — whole-dollar with comma thousands, no decimals.
    // Use case: equity totals, large dollar amounts where cents are noise.
    QF.dollars0 = function (n) {
        var v = _toNum(n);
        if (v === null) return '';
        return '$' + Math.round(v).toLocaleString();
    };

    // "+$12,345" / "-$5,432" — signed whole-dollar with comma thousands.
    // Use case: P&L (positive vs negative important to highlight).
    QF.signedDollars0 = function (n) {
        var v = _toNum(n);
        if (v === null) return '';
        var sign = v >= 0 ? '+$' : '-$';
        return sign + Math.abs(v).toLocaleString(undefined, {
            maximumFractionDigits: 0,
        });
    };

    // "+$12,345.67" / "-$1,234.56" — signed dollars with 2 decimals.
    // Use case: per-trade P&L where cents matter.
    QF.signedDollars2 = function (n) {
        var v = _toNum(n);
        if (v === null) return '';
        var sign = v >= 0 ? '+$' : '-$';
        return sign + Math.abs(v).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    };

    // "12,345" — plain integer with comma thousands.
    // Use case: counts (initial_claims_4wk_avg, page-view averages).
    QF.intCommas = function (n) {
        var v = _toNum(n);
        if (v === null) return '';
        return v.toLocaleString();
    };

    // "12.3%" — non-negative percentage with `dp` decimals (default 1).
    // Use case: win rates, IV ranks.
    QF.percent = function (n, dp) {
        var v = _toNum(n);
        if (v === null) return '';
        if (dp == null) dp = 1;
        return v.toFixed(dp) + '%';
    };

    // "+1.2%" / "-0.5%" — signed percentage with `dp` decimals (default 1).
    // Use case: returns, deltas vs benchmark.
    QF.signedPct = function (n, dp) {
        var v = _toNum(n);
        if (v === null) return '';
        if (dp == null) dp = 1;
        return (v >= 0 ? '+' : '') + v.toFixed(dp) + '%';
    };

    root.QF = QF;
})(window);
