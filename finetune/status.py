"""Where the owned model stands — the one record the app shows.

`status.json` (beside this file) is updated with every batch verdict,
in the same change as the `docs/27` log entry. The Learning page reads
it through `load_status()` so what the operator sees in the app is the
same record the docs carry — the fine-tune track had three failed
rounds and a training defect that were visible only in the docs, which
left "the model is not in use and has not been shown to work"
invisible in the product.

Fails VISIBLY: a missing or malformed file returns an `error` the page
renders; it never falls back to an empty or optimistic panel.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "status.json")
_REQUIRED = ("updated", "in_use", "headline", "bar", "rounds", "next")
_ROUND_KEYS = ("round", "date", "studied", "exam", "ours_pct",
               "untrained_pct", "result")


def load_status(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or STATUS_PATH
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error("finetune status unreadable at %s: %s: %s",
                     path, type(exc).__name__, exc)
        return {"error": "the status record could not be read"}
    missing = [k for k in _REQUIRED if k not in data]
    if not missing and isinstance(data.get("rounds"), list):
        for r in data["rounds"]:
            missing += [f"round.{k}" for k in _ROUND_KEYS if k not in r]
    else:
        missing = missing or ["rounds"]
    if missing:
        logger.error("finetune status at %s is incomplete: missing %s",
                     path, missing)
        return {"error": "the status record is incomplete"}
    return data
