"""2026-06-10 — sync.sh hardening pins.

Multiple times today the operator ran `bash sync.sh`, saw it print
"Both services running.", and trusted that the deploy was live.
It wasn't — prod's git tracker was 4 commits behind origin/main,
and the bug-fix commits I'd just pushed weren't actually exercised
by the next scheduler cycle. Result: the operator saw the same
bugs ("Unsupported option_strategy: 'bull_put_spread'") AFTER
multiple "successful" deploys.

Root causes:

  1. The pre-2026-06-10 script exit 0'd in the rsync-no-changes
     path BEFORE running the .git/ alignment block. If rsync saw
     no file delta (because a previous deploy had already pushed
     content), prod.git stayed wherever it was, even if it was
     behind origin/main.

  2. The post-deploy ssh block did `git reset --hard origin/main`
     under `--quiet` + `2>/dev/null` redirects, hiding real errors.
     Combined with `set -e` not always aborting cleanly when the
     ssh heredoc returns 0 after printing to stderr, error states
     never surfaced to the operator.

  3. The success signal was "Both services running." — services
     can run with stale code; that signal didn't prove deploy.

Hardening pinned here:

  - The rsync-no-changes path NO LONGER exits early. It skips the
    rsync transfer but still runs the .git/ alignment + content
    verification.
  - The post-deploy ssh block uses `bash -s <<SSHEOF` (not `ssh
    HOST "..."`) with `set -euo pipefail` AND `|| { exit 1; }` on
    the outer-shell side, so failures inside the heredoc reach the
    outer script's exit code.
  - Content verification: trade_pipeline.py's sha256 must match
    between local and prod after the reset. Catches the case where
    HEAD looks right but file content somehow doesn't.
  - A `.deploy_sha` marker is written on prod so other tooling can
    confirm deploy state without depending on git internals.
  - The final services-check ALSO re-verifies the marker + content
    hash, so "Both services running." is only printed when deploy
    is provably current.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_SH = REPO_ROOT / "sync.sh"


def _src() -> str:
    return SYNC_SH.read_text()


class TestSyncShHardening:

    def test_no_early_exit_in_no_changes_path(self):
        """The pre-fix script exit 0'd before the .git/ alignment
        when rsync detected no file delta. That path must now FALL
        THROUGH to the post-deploy block."""
        src = _src()
        # Find the no-changes branch
        anchor = src.find('if [ -z "$CHANGED" ]')
        assert anchor > 0, "no-changes branch missing"
        # The 30 lines after the anchor must NOT contain `exit 0`
        # before the post-deploy section. Look for SKIP_RSYNC=true
        # as the new behavior marker.
        window = src[anchor:anchor + 2000]
        assert "exit 0" not in window, (
            "no-changes path must NOT exit early. Pre-fix it did, "
            "skipping the .git/ alignment and leaving prod git "
            "tracker silently stale. Fix: set SKIP_RSYNC=true and "
            "fall through to the post-deploy block."
        )
        assert "SKIP_RSYNC=true" in window, (
            "no-changes path must set SKIP_RSYNC=true so the rsync "
            "transfer is skipped but the .git/ alignment still runs."
        )

    def test_post_deploy_uses_bash_heredoc_with_pipefail(self):
        """The post-deploy ssh block must use `bash -s <<SSHEOF`
        with `set -euo pipefail` AND a `|| { exit 1; }` chained on
        the ssh invocation. Pre-fix used `ssh HOST "..."` style
        with `set -e` inside, which has known silent-failure modes
        when stderr is printed but ssh still returns 0."""
        src = _src()
        anchor = src.find("Aligning prod .git/ to origin/main")
        assert anchor > 0
        # Look at the next ~3000 chars
        window = src[anchor:anchor + 3500]
        assert "bash -s <<SSHEOF" in window, (
            "Post-deploy must use `bash -s <<SSHEOF` heredoc form. "
            "The old `ssh HOST \"...\"` form had silent-failure modes."
        )
        assert "set -euo pipefail" in window, (
            "The heredoc must `set -euo pipefail` so unset vars and "
            "pipeline failures abort cleanly."
        )
        assert "|| {" in window or "|| exit 1" in window, (
            "The ssh invocation must be chained with `|| { ...; "
            "exit 1; }` (or `|| exit 1`) so failure inside the "
            "heredoc reaches the outer script."
        )

    def test_post_deploy_does_content_verification(self):
        """Beyond `git rev-parse HEAD`, the script must verify that
        a representative production file's sha256 matches between
        local and prod. Catches the case where HEAD is correct but
        file content somehow doesn't match (rsync race,
        permissions blip, etc.)."""
        src = _src()
        anchor = src.find("Aligning prod .git/ to origin/main")
        window = src[anchor:anchor + 4000]
        assert "LOCAL_SHA=" in window, (
            "Pre-deploy must compute the local file's sha256 to "
            "compare against prod."
        )
        assert "sha256sum" in window or "shasum" in window, (
            "Post-deploy SSH must compute sha256 of the verify "
            "file on prod."
        )
        # The verification file should be a real production source
        # — trade_pipeline.py is touched by most deploys
        assert "trade_pipeline.py" in window, (
            "The verify file should be trade_pipeline.py — it's "
            "modified by enough deploys to detect stale ones."
        )

    def test_deploy_marker_written_on_prod(self):
        """After successful deploy, prod must have `.deploy_sha`
        and `.deploy_timestamp` files written. Other tooling
        (and the final-verification block) reads `.deploy_sha`
        to confirm state without depending on git internals."""
        src = _src()
        assert ".deploy_sha" in src, (
            "Post-deploy must write .deploy_sha so deploy state "
            "is observable without git commands."
        )
        assert ".deploy_timestamp" in src, (
            "Post-deploy must write .deploy_timestamp so the "
            "operator can see WHEN the current deploy happened."
        )

    def test_final_verification_checks_deploy_marker(self):
        """The final services-check block must verify the
        .deploy_sha marker matches expected and re-hash the
        content file. 'Both services running.' must only print
        when those pass — services-running alone is not a
        sufficient signal of deploy success."""
        src = _src()
        # Find the final verification block
        anchor = src.find("Final verification")
        assert anchor > 0, "Final verification anchor missing"
        window = src[anchor:anchor + 3000]
        assert ".deploy_sha" in window, (
            "Final verification must read .deploy_sha and check it "
            "matches LOCAL_HEAD. Without this, 'services running' "
            "can be true on stale code."
        )
        assert "PROD_SHA" in window and "LOCAL_SHA" in window, (
            "Final verification must re-hash the verify file on "
            "prod and compare to local SHA, defense against drift "
            "between deploy and the final check."
        )
        assert "Both services running" in window, (
            "The 'Both services running.' success message must live "
            "INSIDE the final-verification block, only printed when "
            "all prior checks pass."
        )

    def test_no_quiet_flags_hiding_git_errors(self):
        """The post-deploy ssh block must NOT use `--quiet` on git
        fetch/reset/etc. Those flags suppress error output and
        helped the silent-failure mode."""
        src = _src()
        anchor = src.find("Aligning prod .git/ to origin/main")
        # Window covers just the post-deploy ssh block (~80 lines)
        end = src.find("# Determine what needs restarting", anchor)
        block = src[anchor:end if end > 0 else anchor + 4000]
        # `git fetch origin --quiet` was the prior anti-pattern.
        # The new form must use `git fetch origin` (no --quiet).
        assert "git fetch origin --quiet" not in block, (
            "git fetch origin --quiet hides errors. Use `git fetch "
            "origin` so real errors print."
        )
        # Same for >/dev/null on git operations
        assert "git reset --hard origin/main >/dev/null" not in block, (
            "git reset --hard >/dev/null hides errors. Drop the "
            "redirect."
        )
