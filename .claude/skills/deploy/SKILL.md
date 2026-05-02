---
name: deploy
description: Deploy code or config changes to the locally-running energy-optimiser Docker stack. Use when the user types /deploy or asks to "ship", "deploy", "restart", "bounce", "apply config", or "rebuild the container". Knows the bind-mount layout (config.toml + dashboard static = no rebuild; Python source = rebuild required) so it picks the minimal viable path.
---

# deploy

Deploys local changes to the running stack. The repo isn't a CI-driven environment — `main` is the deployed branch and the host running this skill is the production host. There's no rollback button beyond `git revert` + redeploy, so think before you ship.

## Bind-mount cheat-sheet (REQUIRED reading before choosing the path)

`docker-compose.yml` mounts these from the host into `energy-optimiser`:

| Host path | Container path | Why bind-mounted | Path you take |
|---|---|---|---|
| `./config.toml` | `/etc/energy-optimiser/config.toml` | Tunable knobs without rebuild | **restart-only** (a) |
| `./src/optimiser/api/static/` | `/app/dashboard-static/` | Dashboard handler reads files per-request | **no action** (c) |
| `optimiser-data` named volume | `/var/lib/energy-optimiser/` | DuckDB + snapshots; persistent | n/a |

Everything else under `src/optimiser/` (Python source) is **baked into the image**. The watchdog container uses the same image — Python changes touch both.

## Decide the path

Identify what changed since the last deploy. `git diff HEAD --stat` (compared to whatever's running) is the cleanest signal — but if you've already bounced since the last commit, look at the unstaged + staged diff.

| Change set | Path | Wall-clock |
|---|---|---|
| Only `config.toml` | (a) Restart-only | ~5 s |
| Only `src/optimiser/api/static/` (HTML/CSS/JS) | (c) No action | 0 s |
| Any `src/optimiser/**/*.py` (anything else under `src/optimiser/`) | (b) Rebuild + recreate | ~30–90 s |
| Mix of (a) + (b) | (b) covers it | ~30–90 s |
| `docker-compose.yml`, `Dockerfile`, `pyproject.toml`, `uv.lock` | (b) plus consider `--no-cache` if the dep set changed materially | ~60–180 s |

If you can't tell (e.g. uncommitted noise across many files), prefer (b) — wasting 60 s on an unnecessary rebuild beats the crash-loop you get when the image's `config.py` doesn't know a new `[planner]` field. The 2026-04-29 wear-cost ship learnt this — `config.toml` was bind-mounted but the new `lp_wear_cost_per_kwh` field hadn't been baked into the image, so the running optimiser hit `TypeError: PlannerConfig.__init__() got an unexpected keyword argument` on next start.

**Iron rule:** if a config knob refers to a Python field you just added to `PlannerConfig`/`BatteryConfig`/etc., that's path (b), not (a).

## Pre-flight checks (do these first, every time)

Run all four in parallel before touching the stack:

```bash
# 1. Tests must pass — the container will crash if they don't
cd /home/dudley/code/energy-optimiser && uv run pytest tests/ -q

# 2. Diff vs the deployed branch (assumes main is deployed)
git status -uno
git diff HEAD --stat

# 3. Are both containers currently healthy? Don't deploy on top of an
#    already-broken stack — investigate first.
docker ps --filter name=energy-optimiser --format "{{.Names}} {{.Status}}"

# 4. Is anything actively wrong? Recent logs from both containers.
docker logs --tail 30 energy-optimiser 2>&1 | grep -iE "error|fallback|circuit|exception" | tail -10
docker logs --tail 30 energy-optimiser-watchdog 2>&1 | grep -iE "error|stale|fallback|exception" | tail -5
```

Stop and report if:
- Tests fail (don't ship broken code)
- Either container is restarting / unhealthy (different problem; deploy won't fix it and may mask it)
- The watchdog is currently in a stale-heartbeat loop (means main service is already down — investigate before adding a deploy on top)

## (a) Restart-only — `config.toml` change with no schema change

```bash
cd /home/dudley/code/energy-optimiser
docker compose restart optimiser
```

Then verify (see "Verify the deploy" below).

The compose **service** name is `optimiser`; the **container** name is `energy-optimiser`. The two are NOT interchangeable to `docker compose`. If you `docker compose restart energy-optimiser` you'll get `no such service`.

The watchdog (`energy-optimiser-watchdog`) doesn't read `config.toml` — its inverter coordinates live in `docker-compose.yml` env vars. Restart-only is main-container-only.

## (b) Rebuild + recreate — Python or dep change

```bash
cd /home/dudley/code/energy-optimiser
docker compose build optimiser   # ~30–90s; uv layer caches if pyproject.toml unchanged
docker compose up -d optimiser   # 'up' (not 'restart') so the new image is picked up
```

`docker compose restart` does NOT pick up a new image — it restarts the existing container instance. Always `up -d` after a build.

The watchdog uses the same image but its `command:` is `eo-watchdog`, not the main entrypoint. Python changes that *only* touch `src/optimiser/watchdog.py` should also `docker compose up -d --build watchdog`. Changes anywhere else don't require touching the watchdog (it imports a thin slice). If you're unsure, bounce both:

```bash
docker compose up -d --build optimiser watchdog
```

The `unless-stopped` policy means both containers come back automatically across host reboots — don't add `--restart` flags.

## (c) Static assets only

No action. `EO_DASHBOARD_STATIC_DIR=/app/dashboard-static` and the host directory is bind-mounted; the dashboard handler re-reads files on every request. Hard-refresh the browser (Cmd-R / Ctrl-F5) to bust the browser cache.

## Verify the deploy (every path)

Always do this — the cost of an extra 30 s of verification beats a silent regression that doesn't surface until the next evening peak.

```bash
# 1. Container is up, not restart-looping
docker ps --filter "name=energy-optimiser$" --format "{{.Status}}"
# Expected: "Up <N> seconds" — NOT "Restarting"

# 2. Service reached ACTIVE state in the new run
docker logs --since 2m energy-optimiser 2>&1 | grep -E "state_transition|State:" | tail -3
# Expected: "initialise → active (startup complete)"

# 3. The first tick under the new code/config produced a clean LP solve.
#    until-loop polls /plan/current — first poll right after restart usually
#    pre-dates the first tick, so wait for the snapshot timestamp to be post-restart.
RESTART_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
until curl -fsS -H "Authorization: Bearer $(grep ^EO_API_TOKEN /home/dudley/code/energy-optimiser/.env | cut -d= -f2-)" \
    http://localhost:8090/plan/current 2>/dev/null \
  | jq -e --arg t "$RESTART_TS" '.timestamp > $t' >/dev/null; do sleep 2; done
# Then check that solve:
curl -s -H "Authorization: Bearer $(grep ^EO_API_TOKEN /home/dudley/code/energy-optimiser/.env | cut -d= -f2-)" \
    http://localhost:8090/plan/current \
  | jq '{ts: .timestamp, reason: .lp_solution.reason, fallback: (.output.reason | startswith("fallback") or startswith("lp fallback"))}'
# Expected: reason starts with "LP optimal", fallback=false
```

If the LP starts in fallback (`output.reason` starts with `fallback:` or `lp fallback:`), the tick happened but something inside it tripped — pull longer logs and investigate. Don't redeploy on top.

## When something goes wrong

The 2026-04-29 ship is the worked case study: `config.toml` had a new key the running image didn't recognize, so `docker compose restart` crash-looped the service. Recovery was `docker compose stop optimiser` (stops the loop), `docker compose build optimiser` (gets the new code into the image), `docker compose up -d optimiser` (recreates with the new image).

General playbook for a crash loop:

1. **Stop the loop first** — `docker compose stop optimiser`. While the loop is running, every restart attempt churns logs and tries Modbus reconnects. The watchdog's separate failure domain handles inverter safety; the main container being stopped for ~60 s while you fix things is fine (it'll go to the watchdog's safe state — mode 2, export 0, remote_ems 1 — within ~90 s).
2. **Read the actual error** — `docker logs --tail 30 energy-optimiser 2>&1 | tail -25`. Don't guess.
3. **Pick the fix path:**
   - Stale image vs new config → rebuild (path b)
   - Broken code → revert the offending commit, then path (b)
   - Bad config value → fix `config.toml`, path (a)
4. **Bring it back up** — `docker compose up -d optimiser` (or `--build` if the image needs refresh).
5. **Verify** as above.

If recovery takes >2 minutes total, the watchdog will already have pinned the inverter to the safe state (mode 2 + relays off). That's by design — let it; don't fight it. The optimiser will resume control on its next tick once it's back up.

## Don't

- Don't `docker compose down`. That stops the watchdog too, removing the safety layer for the duration of the deploy. Use `restart` or `up -d` (which only acts on the listed services).
- Don't `--no-deps` to skip the watchdog's compose handling. The default behaviour is correct — `up -d optimiser` doesn't touch the watchdog because the watchdog's `depends_on: optimiser` is a startup-order hint only, not a co-restart trigger.
- Don't run `docker system prune` or `docker image prune` mid-deploy. The previous image is your only fast rollback if the new one is broken — `docker compose up -d` immediately tags `:latest` to the new build, but the old image hangs around with its own SHA. `docker images` still lists it.
- Don't deploy with uncommitted changes unless you've verified by reading the actual diff that you understand what's shipping. The auto-memory and auto-context don't replace `git diff`.
- Don't skip the verification step. "Container says Up" ≠ "LP is solving cleanly". The fallback-on-startup path is silent in `docker ps`.

## When to invoke

Use `/deploy` when the user is:

- Ready to ship a config change (`lp_wear_cost_per_kwh`, `lp_scenario_weight_*`, etc.)
- Ready to ship a Python change after tests have passed
- Asking "can you bounce the container?" / "restart the service" / "apply the new config"
- Recovering from a crash loop they triggered (point them at the playbook above)

Do NOT use this skill for:

- Health checks → `/review-services`
- "What's the LP doing?" → `/explain-plan`
- Tuning iterations / A/B → `/sim-sweep` (validate before deploy, then come here)
- Code changes that haven't been written yet — finish the change + tests first
