"""Pin `_build_page_links` — the page-bar layout helper for /trades
numbered pagination (2026-05-11 TODO #2).

Returns the sequence of page-link entries the template renders:
  - int N           → clickable page link (or active highlight if N == current)
  - None            → ellipsis gap

Always includes page 1 and the last page. Window of N pages around
the current page (default N=2). Gaps wider than 1 between adjacent
visible numbers become an ellipsis.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from views import _build_page_links


class TestSmallTotals:
    def test_single_page(self):
        assert _build_page_links(1, 1) == [1]

    def test_two_pages_no_ellipsis(self):
        assert _build_page_links(1, 2) == [1, 2]

    def test_five_pages_all_visible(self):
        # Window of 2 around current=1 covers 1,2,3. Plus last=5.
        # 1,2,3,4,5 all consecutive → no ellipsis
        assert _build_page_links(1, 5) == [1, 2, 3, 4, 5]


class TestWideTotalsCurrentNearStart:
    def test_current_1_total_20(self):
        # Window: 1,2,3. Plus first=1, last=20.
        # Result: 1,2,3, ..., 20
        assert _build_page_links(1, 20) == [1, 2, 3, None, 20]

    def test_current_3_total_20(self):
        # Window: 1,2,3,4,5. Plus first=1, last=20.
        # Result: 1,2,3,4,5, ..., 20
        assert _build_page_links(3, 20) == [1, 2, 3, 4, 5, None, 20]


class TestWideTotalsCurrentInMiddle:
    def test_current_10_total_20(self):
        # Window: 8,9,10,11,12. Plus first=1, last=20.
        # Result: 1, ..., 8,9,10,11,12, ..., 20
        assert _build_page_links(10, 20) == [
            1, None, 8, 9, 10, 11, 12, None, 20,
        ]


class TestWideTotalsCurrentNearEnd:
    def test_current_18_total_20(self):
        # Window: 16,17,18,19,20. Plus first=1, last=20.
        # Result: 1, ..., 16,17,18,19,20
        assert _build_page_links(18, 20) == [
            1, None, 16, 17, 18, 19, 20,
        ]

    def test_current_20_total_20(self):
        # Window: 18,19,20. Plus first=1, last=20.
        # Result: 1, ..., 18, 19, 20
        assert _build_page_links(20, 20) == [1, None, 18, 19, 20]


class TestWindowParameter:
    def test_window_0_just_current_first_last(self):
        # Window=0: only current page in the window
        # Plus first=1 and last=20. current=10 → [1, ..., 10, ..., 20]
        assert _build_page_links(10, 20, window=0) == [
            1, None, 10, None, 20,
        ]

    def test_window_5_wide(self):
        # Window=5 around current=10 → 5..15. Plus first=1, last=20.
        # Result: 1, ..., 5,6,7,8,9,10,11,12,13,14,15, ..., 20
        result = _build_page_links(10, 20, window=5)
        assert result == [1, None, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                          15, None, 20]


class TestNoEllipsisWhenAdjacent:
    """When the window touches page 1 or the last page, no ellipsis
    should appear (the pages are consecutive)."""
    def test_window_touches_first(self):
        # current=4, window=2 → 2,3,4,5,6. Plus 1, last=20.
        # 1,2,3,4,5,6 consecutive → no ellipsis between
        # 6 to 20 → ellipsis
        assert _build_page_links(4, 20) == [
            1, 2, 3, 4, 5, 6, None, 20,
        ]

    def test_window_touches_last(self):
        # current=17, window=2, total=20 → 15,16,17,18,19,20. Plus 1.
        # 1 alone, then gap, then 15-20
        assert _build_page_links(17, 20) == [
            1, None, 15, 16, 17, 18, 19, 20,
        ]
