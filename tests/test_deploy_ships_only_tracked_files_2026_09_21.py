"""2026-09-21 — a deploy ships exactly what git tracks, and never deletes.

`sync.sh` used to `rsync --delete` the whole working tree minus a
hand-kept exclude list (kept twice). Every deploy therefore DELETED
whatever existed only on the droplet and was not on that list — the
learning archive once (2026-08), then `deploy_logs/` (droplet-side suite
and droplet-sync logs), `.medals_cache.json`, and the altdata scrapers'
caches (395 downloaded House PTR PDFs + 655 price CSVs, re-downloaded by
the next cron run after every deploy) — and UPLOADED whatever was
untracked on the Mac over prod's copy (`.pytest_cache`, the Mac's stale
scraper cache). A denylist fails open. The deploy now ships an allowlist
— `git ls-files` — with no `--delete`; files removed from the repo are
removed on prod by the `git reset --hard origin/main` that already runs.

Pinned structurally AND by running the script's own rsync command
against a real git repo and a stand-in droplet directory.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_SH = os.path.join(REPO, "sync.sh")


def _src() -> str:
    with open(SYNC_SH) as fh:
        return fh.read()


def _code_lines(src: str):
    return [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]


class TestStructure:
    def test_no_rsync_in_the_deploy_can_delete(self):
        for ln in _code_lines(_src()):
            assert "--delete" not in ln, (
                f"`{ln.strip()}` — a deploy must never delete on the "
                "droplet; removals come from git reset on prod")

    def test_there_is_no_exclude_list_left_to_forget(self):
        assert not [ln for ln in _code_lines(_src()) if "--exclude" in ln]

    def test_the_ship_list_is_git_ls_files(self):
        src = _src()
        assert 'ls-files -z > "$SHIP_LIST"' in src
        assert '--from0 --files-from="$SHIP_LIST"' in src
        assert "--checksum" in src

    def test_one_definition_serves_the_dry_run_and_the_real_transfer(self):
        """The exclude list was kept twice and the copies drifted."""
        src = _src()
        assert src.count("RSYNC_SHIP=(") == 1
        uses = [ln for ln in _code_lines(src) if '"${RSYNC_SHIP[@]}"' in ln]
        assert len(uses) == 2 and sum("--dry-run" in u for u in uses) == 1
        # no OTHER rsync invocation (a line that RUNS rsync — not an
        # echo that merely mentions it)
        others = [ln for ln in _code_lines(src)
                  if re.match(r"\s*(\w+=\$\()?\s*rsync\s+-", ln)]
        assert others == [], others

    def test_a_failed_dry_run_stops_the_deploy(self):
        """It used to be swallowed (2>/dev/null … || true) and read as
        "nothing changed"."""
        src = _src()
        i = src.index('if ! DRY_RUN_OUT=$("${RSYNC_SHIP[@]}" --dry-run')
        block = src[i:i + 500]
        assert "exit 1" in block and "2>/dev/null" not in block

    def test_tracked_file_removals_still_reach_prod(self):
        assert "git reset --hard origin/main" in _src()

    def test_an_empty_ship_list_refuses_to_deploy(self):
        src = _src()
        i = src.index('if [ ! -s "$SHIP_LIST" ]')
        assert "exit 1" in src[i:i + 200]


def _ship_command(ship_list: str):
    """The script's own rsync command, with its variable filled in."""
    m = re.search(r"^RSYNC_SHIP=\((.*)\)\s*$", _src(), re.M)
    assert m, "RSYNC_SHIP definition not found"
    return shlex.split(m.group(1).replace('"$SHIP_LIST"', shlex.quote(ship_list)))


@pytest.fixture
def mac_and_droplet(tmp_path):
    mac = tmp_path / "mac"
    droplet = tmp_path / "droplet"
    (mac / "templates").mkdir(parents=True)
    (mac / "altdata" / "congresstrades" / "data" / "cache").mkdir(parents=True)
    (mac / ".pytest_cache").mkdir()
    (mac / "deploy_logs").mkdir()
    (mac / "app.py").write_text("print('v2')\n")
    (mac / "templates" / "page.html").write_text("<p>v2</p>\n")
    # untracked on the Mac — must NOT ship
    (mac / ".pytest_cache" / "junk").write_text("junk")
    (mac / "deploy_logs" / "sync-mac.log").write_text("mac log")
    (mac / "altdata" / "congresstrades" / "data" / "cache" /
     "pdf_stale.pdf").write_text("stale mac copy")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "app.py", "templates"],
                ["git", "commit", "-q", "-m", "v2"]):
        subprocess.run(cmd, cwd=mac, check=True, env=env,
                       capture_output=True)
    # the droplet: an older deploy plus state that exists ONLY there
    (droplet / "templates").mkdir(parents=True)
    (droplet / "deploy_logs").mkdir()
    (droplet / "altdata" / "congresstrades" / "data" / "cache").mkdir(
        parents=True)
    (droplet / ".cache" / "french_factors").mkdir(parents=True)
    (droplet / "app.py").write_text("print('v1')\n")
    (droplet / "deploy_logs" / "suite-droplet.log").write_text("7160 passed")
    (droplet / ".medals_cache.json").write_text('{"1": {}}')
    (droplet / "altdata" / "congresstrades" / "data" / "cache" /
     "pdf_2026_1.pdf").write_text("fresh droplet download")
    (droplet / ".cache" / "french_factors" / "ff5_daily.csv").write_text("x")
    (droplet / "quantopsai_profile_229.db").write_text("live journal")
    return mac, droplet


def _deploy(mac, droplet, tmp_path):
    ship = tmp_path / "ship.list"
    with open(ship, "wb") as fh:
        fh.write(subprocess.run(["git", "ls-files", "-z"], cwd=mac,
                                check=True, capture_output=True).stdout)
    subprocess.run(_ship_command(str(ship)) + [f"{mac}/", f"{droplet}/"],
                   check=True, capture_output=True)


class TestBehaviour:
    def test_droplet_only_state_survives_a_deploy(self, mac_and_droplet,
                                                  tmp_path):
        mac, droplet = mac_and_droplet
        _deploy(mac, droplet, tmp_path)
        assert (droplet / "deploy_logs" / "suite-droplet.log"
                ).read_text() == "7160 passed"
        assert (droplet / ".medals_cache.json").exists()
        assert (droplet / "altdata/congresstrades/data/cache/pdf_2026_1.pdf"
                ).read_text() == "fresh droplet download"
        assert (droplet / ".cache/french_factors/ff5_daily.csv").exists()
        assert (droplet / "quantopsai_profile_229.db"
                ).read_text() == "live journal"

    def test_tracked_files_are_shipped(self, mac_and_droplet, tmp_path):
        mac, droplet = mac_and_droplet
        _deploy(mac, droplet, tmp_path)
        assert (droplet / "app.py").read_text() == "print('v2')\n"
        assert (droplet / "templates" / "page.html").read_text() == "<p>v2</p>\n"

    def test_untracked_mac_files_never_reach_the_droplet(
            self, mac_and_droplet, tmp_path):
        mac, droplet = mac_and_droplet
        _deploy(mac, droplet, tmp_path)
        assert not (droplet / ".pytest_cache").exists()
        assert not (droplet / "deploy_logs" / "sync-mac.log").exists()
        assert not (droplet / "altdata/congresstrades/data/cache/pdf_stale.pdf"
                    ).exists()

    def test_change_detection_is_by_content_not_timestamp(
            self, mac_and_droplet, tmp_path):
        """prod's files are rewritten by `git reset`, so their mtimes
        never match the Mac's; by-mtime detection re-sent (and the
        restart logic "saw a change in") nearly every file."""
        mac, droplet = mac_and_droplet
        _deploy(mac, droplet, tmp_path)
        os.utime(droplet / "app.py", (1, 1))            # mtime now differs
        ship = tmp_path / "ship2.list"
        with open(ship, "wb") as fh:
            fh.write(subprocess.run(["git", "ls-files", "-z"], cwd=mac,
                                    check=True, capture_output=True).stdout)
        out = subprocess.run(
            _ship_command(str(ship)) + ["--dry-run", "--itemize-changes",
                                        f"{mac}/", f"{droplet}/"],
            check=True, capture_output=True, text=True).stdout
        assert [ln for ln in out.splitlines() if ln.startswith("<f")] == []
