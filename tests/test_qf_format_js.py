"""Pin static/js/format.js (window.QF.*) output for every helper.

Issue 13 (2026-05-10): consolidated 4+ inconsistent inline `function
fmt*` declarations into a single shared helper at static/js/format.js.
This test pins the output of each helper so future edits don't
silently change the format that templates depend on.

Runs the JS module via Node (or Python's `subprocess` invoking node)
and asserts the returned strings exactly. Skipped when node is not
installed (CI environments without node still pass; locally and in
deploy environments where node IS available, format drift breaks
the test).
"""

import os
import shutil
import subprocess
import sys

import pytest


FORMAT_JS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "static", "js", "format.js",
)


@pytest.fixture(scope="module")
def node_runner():
    """Skip these tests gracefully if node isn't installed."""
    if shutil.which("node") is None:
        pytest.skip("node not installed; QF JS helpers tested via "
                    "deploy + manual verification")
    return shutil.which("node")


def _run_qf(node_runner, expr):
    """Eval an expression in the QF namespace and return the printed
    result. Loads format.js via a wrapper that polyfills `window`."""
    with open(FORMAT_JS_PATH) as f:
        format_src = f.read()
    wrapper = f"""
    var window = {{}};
    {format_src};
    var QF = window.QF;
    process.stdout.write(String({expr}));
    """
    res = subprocess.run(
        [node_runner, "-e", wrapper],
        capture_output=True, text=True, timeout=10,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"node failed (rc={res.returncode}):\n{res.stderr}"
        )
    return res.stdout


class TestDollars2:
    """`$12,345.67` — currency, comma thousands, exactly 2 decimals."""

    def test_basic(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars2(12345.6789)") == "$12,345.68"

    def test_zero(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars2(0)") == "$0.00"

    def test_negative_keeps_minus_inside_dollar(self, node_runner):
        # toLocaleString puts the minus sign before the number, before $
        # We accept either "$-1.23" or "-$1.23" as long as it's parseable
        out = _run_qf(node_runner, "QF.dollars2(-1.23)")
        assert "1.23" in out and "-" in out

    def test_null_returns_empty(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars2(null)") == ""

    def test_undefined_returns_empty(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars2(undefined)") == ""


class TestDollars0:
    """`$12,345` — whole-dollar, comma thousands."""

    def test_basic(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars0(12345.99)") == "$12,346"

    def test_rounds_half_up(self, node_runner):
        # JS Math.round rounds .5 toward +inf
        assert _run_qf(node_runner, "QF.dollars0(0.5)") == "$1"

    def test_zero(self, node_runner):
        assert _run_qf(node_runner, "QF.dollars0(0)") == "$0"


class TestSignedDollars:
    def test_positive_signed_dollars0(self, node_runner):
        assert _run_qf(node_runner, "QF.signedDollars0(12345)") == "+$12,345"

    def test_negative_signed_dollars0(self, node_runner):
        assert _run_qf(node_runner, "QF.signedDollars0(-12345)") == "-$12,345"

    def test_zero_signed_dollars0(self, node_runner):
        assert _run_qf(node_runner, "QF.signedDollars0(0)") == "+$0"

    def test_signed_dollars2(self, node_runner):
        assert _run_qf(node_runner, "QF.signedDollars2(1234.5)") == "+$1,234.50"
        assert _run_qf(node_runner, "QF.signedDollars2(-78.9)") == "-$78.90"


class TestIntCommas:
    def test_basic(self, node_runner):
        assert _run_qf(node_runner, "QF.intCommas(1234567)") == "1,234,567"

    def test_negative(self, node_runner):
        out = _run_qf(node_runner, "QF.intCommas(-1234)")
        assert "1,234" in out and "-" in out


class TestPercent:
    def test_default_one_decimal(self, node_runner):
        assert _run_qf(node_runner, "QF.percent(12.345)") == "12.3%"

    def test_explicit_zero_decimals(self, node_runner):
        assert _run_qf(node_runner, "QF.percent(12.7, 0)") == "13%"

    def test_signed_pct_positive(self, node_runner):
        assert _run_qf(node_runner, "QF.signedPct(1.234)") == "+1.2%"

    def test_signed_pct_negative(self, node_runner):
        assert _run_qf(node_runner, "QF.signedPct(-0.5)") == "-0.5%"


class TestNullSafety:
    @pytest.mark.parametrize("fn", [
        "dollars2", "dollars0", "signedDollars0", "signedDollars2",
        "intCommas", "percent", "signedPct",
    ])
    def test_null_returns_empty(self, node_runner, fn):
        assert _run_qf(node_runner, f"QF.{fn}(null)") == ""

    @pytest.mark.parametrize("fn", [
        "dollars2", "dollars0", "signedDollars0", "signedDollars2",
        "intCommas", "percent", "signedPct",
    ])
    def test_non_finite_returns_empty(self, node_runner, fn):
        # Infinity / NaN should not produce 'Infinity' or 'NaN' in the UI
        assert _run_qf(node_runner, f"QF.{fn}(Infinity)") == ""
        assert _run_qf(node_runner, f"QF.{fn}(NaN)") == ""
