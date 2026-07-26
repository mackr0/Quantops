"""Meta retrain gated on enable_meta_model (P1.7, 2026-07-26).

The NoMetaModel ablation arm (p212) had every CONSUMER gated
(pregate + STEP-4.5 both check enable_meta_model) but not the
TRAINER: the daily retrain task built an AUC-0.82 GBM and
bootstrapped the SGD online model for a profile designed to never
use them — wasted compute and a standing risk that any future
ungated reader silently un-ablates the arm.

Pinned here: the retrain task trains nothing for ablation profiles
and self-heals by deleting leftover model files; enabled profiles
still train; and the online model's incremental update path cannot
CREATE a model (only initialize_from_history can, and it lives
behind the same gate) — so file deletion + the gate make the whole
subsystem structurally inert for ablation arms.
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _ctx(enabled):
    ctx = MagicMock()
    ctx.enable_meta_model = enabled
    ctx.profile_id = 212
    ctx.display_name = "NoMetaModel"
    ctx.segment = "test"
    ctx.db_path = "unused.db"
    ctx.user_id = 1
    return ctx


class TestRetrainGate:
    def test_ablation_arm_trains_nothing_and_deletes_leftovers(self):
        import meta_model
        import online_meta_model
        from multi_scheduler import _task_retrain_meta_model
        with tempfile.TemporaryDirectory() as td:
            gbm = os.path.join(td, "meta_model_212.pkl")
            online = os.path.join(td, "online_meta_model_p212.pkl")
            for p in (gbm, online):
                open(p, "wb").write(b"leftover")
            with patch.object(meta_model, "model_path_for_profile",
                              return_value=gbm), \
                 patch.object(online_meta_model, "_model_path",
                              return_value=online), \
                 patch.object(meta_model, "train_and_save") as train:
                _task_retrain_meta_model(_ctx(enabled=False))
            assert not train.called, (
                "the NoMetaModel ablation arm TRAINED a meta-model — "
                "ablation purity broken again."
            )
            assert not os.path.exists(gbm) and not os.path.exists(online), (
                "leftover model files must be deleted (self-healing) so "
                "no ungated reader can ever load them."
            )

    def test_enabled_profile_still_trains(self):
        import meta_model
        from multi_scheduler import _task_retrain_meta_model
        with patch.object(meta_model, "train_and_save",
                          return_value=None) as train:
            _task_retrain_meta_model(_ctx(enabled=True))
        assert train.called


class TestOnlineModelCannotSelfCreate:
    def test_incremental_update_never_creates_a_model(self):
        """resolve_predictions calls update_online_model on every
        resolution regardless of the ablation flag — safe ONLY because
        an update can never create a bundle from nothing."""
        from online_meta_model import update_online_model, _model_path
        with tempfile.TemporaryDirectory() as td:
            ok = update_online_model(
                212, {"score": 3.0}, outcome_label=1, base_dir=td)
            assert ok is False
            assert not os.path.exists(_model_path(212, td))
