# QuantOpsAI — operator rules for every session on this droplet

See `DROPLET_DEV.md` for the full workflow. These are the ones that get broken:

1. **Nothing session-tied, ever.** The suite, a deploy, a repair script AND any
   wait/poll on them run detached — `setsid nohup <cmd> > deploy_logs/<name>.log
   2>&1 < /dev/null &` — and the result is read from the log on a later turn.
   Sessions drop without warning (the operator is on the DigitalOcean browser
   console); a dead wait looks like a dead job.
2. **Commit on the feature branch as soon as the targeted tests pass.** Every
   deploy runs `git reset --hard origin/main` and erases uncommitted edits.
3. **Requested fixes need no go-ahead** — carry them through suite → commit →
   push → `./droplet-sync.sh` → live verification.
4. **No deferring** — build it now or hand off a complete written spec.
5. Zero-fail / zero-skip full suite before push; fail-closed fixes; dated
   CHANGELOG entry + OPEN_ITEMS update; work on a branch.
6. **One thing at a time** when the operator has to act: one short step, then wait.
