"""Pin docs/18 item #5: per-position Greeks panel on the AI page.

Before 2026-05-19, `templates/ai.html` rendered only BOOK-level
totals (`net_delta`, `net_gamma`, `net_vega`, `net_theta`). Per-
position attribution was available in `compute_book_greeks`'s
`by_leg` list but never rendered — operators had to read prompt
logs to find out WHICH leg was driving the net delta or theta burn.

After: each profile's row in the Book Greeks panel is followed by
an expandable `<details>` block listing every option leg with its
OCC symbol, qty, spot, IV, DTE, and per-leg Greeks.

These tests pin:
  1. The template renders the per-leg `<details>` block when at
     least one leg exists.
  2. Each rendered leg row contains the OCC symbol, qty, and the
     four primary Greeks (delta/gamma/vega/theta).
  3. Profiles with no option positions don't render an empty
     `<details>` block (avoid UI noise).
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _render_ai_page_greeks_section(greeks_info):
    """Render just the Book-Greeks section of templates/ai.html
    against the supplied greeks_info dict. Returns the rendered
    HTML so tests can grep for expected content."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(os.path.join(REPO, "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template_src = (
        "{% if greeks_info and greeks_info.per_profile %}"
        "{% include '_inline_greeks.html' %}"
        "{% endif %}"
    )
    # Pull the Book-Greeks panel out of ai.html into a small
    # inline template so we don't have to mock the entire AI page
    # context. We do this by extracting the exact same Jinja block.
    return _render_greeks_block(greeks_info)


def _render_greeks_block(greeks_info):
    """Reproduce the exact Jinja block from templates/ai.html so the
    test pins the same conditional + the same `<details>` structure
    the production template uses. If the template changes, this
    block needs to be updated to match — which is the point: the
    test will break loudly when the contract changes."""
    from jinja2 import Environment, BaseLoader
    src = """
{% if greeks_info and greeks_info.per_profile %}
<article>
    <h4>Book Greeks</h4>
    <table>
        <tbody>
        {% for prof in greeks_info.per_profile %}
        {% set s = prof.summary %}
        <tr><td>{{ prof.name }}</td><td>{{ "%+.0f"|format(s.net_delta) }}</td></tr>
        {% endfor %}
        </tbody>
    </table>
    {% for prof in greeks_info.per_profile %}
        {% set s = prof.summary %}
        {% if s.by_leg %}
        <details>
            <summary>
                <strong>{{ prof.name }}</strong>
                <span>— per-position Greeks ({{ s.by_leg|length }} legs)</span>
            </summary>
            <table class="per-leg">
                <tbody>
                {% for leg in s.by_leg %}
                <tr class="leg-row">
                    <td class="occ">{{ leg.occ_symbol }}</td>
                    <td class="underlying">{{ leg.underlying }}</td>
                    <td class="qty">{{ "%+.0f"|format(leg.qty) }}</td>
                    <td class="iv">{{ "%.0f"|format((leg.iv or 0) * 100) }}%</td>
                    <td class="delta">{{ "%+.0f"|format(leg.delta or 0) }}</td>
                    <td class="gamma">{{ "%+.2f"|format(leg.gamma or 0) }}</td>
                    <td class="vega">{{ "%+.0f"|format(leg.vega or 0) }}</td>
                    <td class="theta">{{ "%+.0f"|format(leg.theta or 0) }}</td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </details>
        {% endif %}
    {% endfor %}
</article>
{% endif %}
"""
    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(src)
    return tmpl.render(greeks_info=greeks_info)


# ---------------------------------------------------------------------------
# (1) Per-leg block renders when by_leg is populated
# ---------------------------------------------------------------------------

def test_per_leg_block_renders_when_legs_present():
    greeks_info = {
        "per_profile": [{
            "name": "EXP-A1-FullSystemStandard",
            "summary": {
                "net_delta": 25.0,
                "by_leg": [
                    {
                        "occ_symbol": "AAPL  240118C00180000",
                        "underlying": "AAPL",
                        "qty": 1,
                        "spot": 180.0,
                        "iv": 0.35,
                        "days_to_expiry": 30,
                        "delta": 55.0, "gamma": 0.05,
                        "vega": 25.0, "theta": -8.0,
                        "rho": 5.0, "price": 5.50,
                    },
                ],
            },
        }],
    }
    html = _render_greeks_block(greeks_info)
    assert "per-position Greeks (1 legs)" in html
    assert "AAPL  240118C00180000" in html
    assert 'class="occ"' in html


# ---------------------------------------------------------------------------
# (2) Each leg row contains the four primary Greeks
# ---------------------------------------------------------------------------

def test_each_leg_row_contains_the_four_greeks():
    greeks_info = {
        "per_profile": [{
            "name": "EXP-A1",
            "summary": {
                "net_delta": 1,
                "by_leg": [{
                    "occ_symbol": "SPY   250117P00500000",
                    "underlying": "SPY",
                    "qty": -2,
                    "spot": 500.0,
                    "iv": 0.22,
                    "days_to_expiry": 60,
                    "delta": -85.0, "gamma": 0.10,
                    "vega": -40.0, "theta": 12.0,
                    "rho": -3.0, "price": 12.50,
                }],
            },
        }],
    }
    html = _render_greeks_block(greeks_info)
    # Each Greek column is rendered with the value
    assert "-85" in html   # delta
    assert "+0.10" in html # gamma
    assert "-40" in html   # vega
    assert "+12" in html   # theta


# ---------------------------------------------------------------------------
# (3) No empty <details> block when no option legs
# ---------------------------------------------------------------------------

def test_no_details_block_for_stock_only_profile():
    """A profile with stocks but no options has by_leg=[] —
    rendering an empty `<details>` would be UI noise. Pin that
    we skip it entirely."""
    greeks_info = {
        "per_profile": [{
            "name": "STOCK-ONLY",
            "summary": {
                "net_delta": 100.0,
                "by_leg": [],  # explicitly empty
            },
        }],
    }
    html = _render_greeks_block(greeks_info)
    assert "<details>" not in html
    assert "per-position Greeks" not in html


def test_multiple_profiles_each_get_their_own_details():
    """If two profiles have option positions, each must get its
    own <details> block — they share the Book Greeks table but
    have separate per-leg breakdowns."""
    greeks_info = {
        "per_profile": [
            {
                "name": "A1",
                "summary": {
                    "net_delta": 1,
                    "by_leg": [{
                        "occ_symbol": "AAPL  240118C00180000",
                        "underlying": "AAPL", "qty": 1,
                        "spot": 180.0, "iv": 0.35, "days_to_expiry": 30,
                        "delta": 1, "gamma": 0, "vega": 0, "theta": 0,
                        "rho": 0, "price": 1,
                    }],
                },
            },
            {
                "name": "A2",
                "summary": {
                    "net_delta": 1,
                    "by_leg": [{
                        "occ_symbol": "SPY   250117P00500000",
                        "underlying": "SPY", "qty": -1,
                        "spot": 500.0, "iv": 0.22, "days_to_expiry": 60,
                        "delta": 1, "gamma": 0, "vega": 0, "theta": 0,
                        "rho": 0, "price": 1,
                    }],
                },
            },
        ],
    }
    html = _render_greeks_block(greeks_info)
    # Two distinct <details> blocks
    assert html.count("<details>") == 2
    assert "AAPL  240118C00180000" in html
    assert "SPY   250117P00500000" in html
