"""The repo-root conftest.py refuses outbound network for every test —
including these `altdata/*/tests` trees, which cannot see fixtures
defined under the main `tests/` directory. Pinned here so the coverage
is proven from INSIDE one of them (see
tests/test_suite_is_hermetic_2026_09_20.py for the mechanism itself).
"""
import socket

import pytest


def test_network_is_refused_in_the_altdata_trees_too():
    with pytest.raises(socket.gaierror, match="network disabled"):
        socket.getaddrinfo("www.sec.gov", 443)
