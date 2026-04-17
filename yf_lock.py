"""Thread-safe wrapper for yfinance.download().

yfinance uses a shared global `_DFS` dict internally that isn't
thread-safe. When multiple profiles call `yf.download()` in parallel
(via ThreadPoolExecutor), it crashes with:
    RuntimeError: dictionary changed size during iteration

This module provides a single global lock that all yf.download calls
must go through.
"""

import threading
import yfinance as yf

_lock = threading.Lock()


def download(*args, **kwargs):
    """Thread-safe wrapper around yf.download()."""
    with _lock:
        return yf.download(*args, **kwargs)
