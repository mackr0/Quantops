# Working directly on the droplet

**What this is.** The normal workflow is: edit on the Mac → `./sync.sh` →
droplet. `sync.sh` is a **Mac-side** tool (`LOCAL_REPO=/Users/mackr0/Quantops`,
rsync → `67.205.155.63`); it cannot run here. When work happens **on the
droplet** (operator away from their computer, or a task that needs the live
DBs / live broker), this file is the equivalent discipline. Established
2026-07-24 during the anonymous-entry-closer investigation.

---

## 1. Environment

```
repo      /opt/quantopsai
python    /opt/quantopsai/venv/bin/python3      # NOT system python
services  quantopsai       (scheduler — multi_scheduler.py)
          quantopsai-web   (gunicorn)
master DB quantopsai.db    (users, alpaca_accounts)
profiles  quantopsai_profile_<id>.db            # 229-240 on accounts 61/62/63 (A1/A2/A3), Experiment 2 from 2026-08-24
```

**Three tools the suite needs. Without them it does NOT run clean, and the
gaps are silent** (they present as skips / a dead guardrail, not errors):

| Tool | Needed by | Without it |
|---|---|---|
| `node` | `tests/test_qf_format_js.py` | **32 tests skip** (they run `static/js/format.js` through node) |
| `pyflakes` | `test_pnl_reconciliation_2026_07_15.py` undefined-names guardrail | guardrail **fails/can't run** (it IS in `requirements.txt`; the venv just lacked it) |
| `gh` | pushing | **cannot push** — the droplet has no other git credential |

Install: `apt-get install -y nodejs gh` · `./venv/bin/pip install pyflakes`

---

## 2. Full suite — the gate

```bash
cd /opt/quantopsai
./venv/bin/python3 -m pytest -q -p no:randomly        # ~17 min
```

Expected: **7,036 passed, 0 failed, 0 skipped** (2026-09-19), in ~11 minutes **when run with its temp files in RAM**:

```bash
mkdir -p /dev/shm/qo-pytest/tmp
TMPDIR=/dev/shm/qo-pytest/tmp setsid nohup ./venv/bin/python3 -m pytest -q -p no:randomly \
    --basetemp=/dev/shm/qo-pytest/base > deploy_logs/suite-<name>.log 2>&1 < /dev/null &
# afterwards: rm -rf /dev/shm/qo-pytest   (peaks ~290MB of the 984MB tmpfs)
```

Why: the droplet's disk costs ~33ms per synced write (measured 2026-09-19; healthy is 1–2ms) and the suite makes tens of thousands of SQLite commits — on disk the same run took 82 minutes at 40% I/O-wait and tripped 30s timeouts in tests that have nothing wrong with them. tmpfs changes where temp files live, not what any test does. Always detached (`setsid nohup`): a session-tied run dies with a dropped connection. If the box is crawling before you start, check the scheduler's swap (`grep VmSwap /proc/$(systemctl show quantopsai -p MainPID --value)/status`) — see OPEN_ITEMS, 2026-09-19.

House rules — all of them, every time:

- **Zero fail. Zero skip.** A failure is never "pre-existing / not mine." A
  skip is not a pass — check what it's hiding.
- **fail-closed** behavior in the fix itself.
- **dated CHANGELOG entry** + OPEN_ITEMS updated.
- develop on a branch; full suite green **before** push.

Use `-p no:randomly` for reproducibility; drop it occasionally, since
`pytest-randomly` is what catches order-dependent flakes.

### The droplet-only failure trap

A test that fails **here but not on the Mac** is almost always a hermeticity
leak — the test reaching *real host state* that doesn't exist on a clean
machine. Seen 2026-07-24 (16 failures, all this class):

- **Real master DB.** Resolvers find `quantopsai.db` via `config.DB_PATH`,
  falling back to `/opt/quantopsai/quantopsai.db` — which **exists here**.
  → Fix: `monkeypatch.setattr(config, "DB_PATH", str(tmp_db))` with an
  **absolute** path (absolute short-circuits the fallback probe).
  Use `market_data.resolve_master_db_path()` in prod code — never a bare
  relative `"quantopsai.db"`, which also breaks in cron CWDs.
  **Never combine that monkeypatch with the `tmp_main_db` fixture** —
  the fixture already sets and restores `config.DB_PATH`, and the two
  undo in the wrong order at teardown, leaving `config.DB_PATH` aimed at
  a dead temp database for every later test (2026-09-20: one such test
  caused ten order-dependent failures elsewhere).
- **Live APIs.** The suite **refuses all outbound network by default**
  (repo-root `conftest.py`, since 2026-09-20): a test that reaches for
  Yahoo, Alpaca, FRED, an LLM vendor or anything else gets an instant
  refusal and exercises the code's outage path. Before that, 132 tests
  made 684 real calls per run, 23 of them GETs to the broker's paper
  API with the live keys. A test that genuinely must reach the network
  opts out with `@pytest.mark.allow_network` — and must be added to the
  allow-list in `tests/test_suite_is_hermetic_2026_09_20.py`, so an
  opt-out is always a reviewed decision. Stubbing at the source module
  (e.g. `options_chain_alpaca.list_available_contracts`) is still the
  way to test a *successful* fetch.
- **Persisted caches.** e.g. `screener._PERSISTED_CACHE_PATH` is a hardcoded
  `/opt/quantopsai/quantopsai.db`. → Patch the reader.

**Fix the isolation — never delete the assertion or skip the test.**

---

## 3. Deploy from the droplet — `./droplet-sync.sh`

**One command (2026-07-24, operator directive — no more hand-derived
steps):**

```bash
cd /opt/quantopsai
git push origin main          # via gh credential helper
./droplet-sync.sh             # the deploy. That's it.
```

`droplet-sync.sh` is `sync.sh` stage for stage — pre-flight gate
(clean tree, HEAD == origin/main), changed-set detection (previous
deployed sha → HEAD), `git reset --hard origin/main`, HEAD + tracked-
drift + content-sha verification, deploy markers, the same restart
patterns with the same scheduler idle-window wait, and the same final
service/marker/rehash verification. The ONLY structural difference:
no Mac→droplet rsync (the code is pushed from here; the laptop catches
up on its next `git pull` + `./sync.sh`). Flags are identical:
`--web` / `--scheduler` / `--all`.

**Both scripts self-detach by default**: the run survives the terminal
closing, output lands in `deploy_logs/<name>-<UTC>.log` (gitignored),
and the launching shell prints the log path immediately. Follow with
`tail -f <log>`; `SYNC_FOREGROUND=1 ./sync.sh` opts out on the Mac.

Prod's `.git` must always track `origin/main`. Deploying un-pushed code
is the silent-revert trap — the pre-flight gate enforces push-first.

Then **verify against the live system** — not just "services are up." Import
the deployed function and replay real journal/broker data through it; prove
the new behavior differs from the old on the actual incident rows.

Logs are journald only (`journalctl -u quantopsai`), and **retention is
short** — a few hours. Forensics older than that must come from the broker
API and the journal DBs, not logs.

---

## 4. Caveats

- **The Mac goes stale.** Every droplet push puts it behind; `git pull` before
  the next `./sync.sh` or its pre-flight stops you (that's it protecting you).
- **`gh` stores the token in plaintext** at `/root/.config/gh/hosts.yml`
  (mode 600, `repo` scope). Convenient; revoke via GitHub → Settings →
  Applications when done, then `gh auth login` next time.
- **`node` / `pyflakes` / `gh` are host-level** — `sync.sh` does not manage
  them and a rebuild won't restore them. Re-install per §1.
- **sshd keepalives are host-level too**: `/etc/ssh/sshd_config.d/`
  `99-session-keepalive.conf` (ClientAliveInterval 60, CountMax 120,
  TCPKeepAlive yes — 2026-07-24, stops idle web/terminal sessions being
  dropped). A rebuilt host loses it; recreate + `systemctl reload ssh`.
- Set git identity if commits fail: the repo uses
  `Claude <noreply@anthropic.com>`.
