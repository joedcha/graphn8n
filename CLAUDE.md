# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`n8n-zabbix-bridge`: connects n8n (Community Edition) to Zabbix so every workflow is automatically monitored, with per-node execution timing and governance auditing. Grafana visualizes the data via the Zabbix datasource — Zabbix does all collection/alerting, Grafana only renders dashboards. Full design rationale, architecture diagram, and roadmap live in `docs/plan-observabilidad-n8n-grafana.md` — read it before making architectural changes.

**Two implementations exist, only one is deployed:**

- **`bridge-python/`** — the one actually running in production (cron on `10.203.25.154`, path `/opt/bmc/ETLs/n8n/monworkflows/`). Ported to Python because that host is RHEL 7.5 with no Node.js (and no realistic way to get modern Node running there — its glibc predates what Node ≥18 requires), no Docker, no git; the only usable runtime is a self-contained portable Python 3.11 (copied from a sibling project, `/opt/bmc/ETLs/loadrancher/python`, into `bridge-python`'s own `python/` subdir so it doesn't depend on that project's lifecycle). See "bridge-python (deployed)" below.
- **`bridge/`** — the original Node.js daemon design. Kept as reference/history; **not deployed**, not being maintained in lockstep with `bridge-python/`. Treat drift between the two as expected, not a bug, unless someone decides to actually run this somewhere with Node available.

If you're asked to change monitoring behavior, change `bridge-python/` — that's what's live.

## bridge-python (deployed)

Same responsibilities as the Node version (discovery/executions/governance against n8n → Zabbix), reimplemented as **stdlib-only Python** (`urllib.request`, no `requests`, matching the Node side's "no external deps" philosophy) and restructured from a long-lived daemon into **one-shot scripts invoked by cron** — the target host has no process supervisor (no systemd unit, no pm2) and no daemon convention; every other ETL in `/opt/bmc/ETLs` is a cron-triggered script that runs and exits.

### Layout and deployment

- `monitor.py` — entrypoint, `--mode {discovery,executions,governance,all}`. `--mode all` runs all three in one process (used for manual/dry-run testing); the real cron uses the three separately.
- `discovery.py`, `executions.py`, `governance.py`, `n8n_client.py`, `zabbix_sender.py` — 1:1 ports of the Node modules, plus the fixes below.
- `run_discovery.sh`, `run_executions.sh`, `run_governance.sh` — cron wrappers matching the host's existing ETL script convention (header block, `logsh.txt` logging, `date` stamps). Cron:
  ```
  */5 * * * * /opt/bmc/ETLs/n8n/monworkflows/run_discovery.sh
  * * * * *   /opt/bmc/ETLs/n8n/monworkflows/run_executions.sh
  0 * * * *   /opt/bmc/ETLs/n8n/monworkflows/run_governance.sh
  ```
- `.env` (chmod 600, never commit) / `.env.example` — same variables as `bridge/.env.example` plus two new ones (see "Pilot mode" and "Per-workflow SLO" below). No `*_INTERVAL_MS` vars — cadence is the crontab, not the script.
- `python/` on the server — the copied portable interpreter. Not present in this repo (618MB, and `.gitignore`d) — if redeploying from scratch, `cp -r /opt/bmc/ETLs/loadrancher/python <this-dir>/python` on the target host.

### Because it's cron, not a daemon: state.json carries more weight

The Node daemon kept `criticalWorkflowIds` in memory, shared between its discovery and executions loops in the same process. Cron runs `discovery` and `executions` as **separate processes**, so that set has to round-trip through `STATE_FILE` (`lastExecutionId`, `criticalNodes`, `criticalWorkflowIds`). Two bugs came directly from this shift and are worth knowing about if you touch state handling:

1. **`executions.py`'s checkpoint save used to overwrite the whole state file** with a fresh dict containing only `lastExecutionId`/`criticalNodes`, silently dropping `criticalWorkflowIds` that `discovery.py` had just written. Since the real cron always runs them as separate processes (unlike `--mode all`, which passes `critical_ids` in memory and never hit this), this meant `ONLY_CRITICAL` filtering saw an empty set on effectively every real tick — the critical workflow's executions were being discarded, not sent. Fixed: `process_executions` now mutates and re-saves the loaded state dict instead of constructing a new one.
2. **No lock shared between `discovery` and `executions`** — each script's `flock` only prevented overlap with itself, not with each other, so a read of `state.json` mid-write by the other script was possible. Both `run_discovery.sh` and `run_executions.sh` now `flock -w 30` the same `.state.lock` file. `governance.py` never touches `STATE_FILE`, so it keeps its own independent lock.

If `ONLY_CRITICAL` workflows stop showing fresh data after future changes, re-check both of these first.

### Executions still in progress get mis-captured if you're not careful

n8n executions paused on a `Wait` node report `status: waiting`/`finished: false`, but **already have `startedAt`/`stoppedAt` set** — querying one mid-wait looks exactly like a finished execution with a very short duration. `executions.py` now checks `status` against `NON_TERMINAL_STATUSES = {'waiting', 'running', 'new'}` and **withholds the checkpoint** at `min_pending_id - 1` whenever a non-terminal execution is seen, so it gets re-fetched (and re-evaluated) on the next tick instead of being locked in with wrong data forever. Trade-off: while something's pending, later terminal executions of the same workflow may get re-sent to Zabbix on every tick until the pending one resolves — harmless for `TRAP` items (just extra identical history points), chosen deliberately over the alternative (silently dropping data).

### `n8n.workflow.duration.last` is computed from node timestamps, not `stoppedAt`

For workflows with `Wait` nodes, the execution's own `startedAt`/`stoppedAt` only reflects the **last leg** since the most recent resume, not the true total span — confirmed empirically (sum of node `executionTime`, including Wait pauses, exceeded the reported `stoppedAt - startedAt` by 300x on a real execution). `compute_duration_ms` now reconstructs the real span as `max(node.startTime + node.executionTime) - min(node.startTime)` across all of `runData`, falling back to `startedAt`/`stoppedAt` only when `runData` isn't available. If a workflow's reported duration ever looks implausibly short again, this is the first thing to check.

### Pilot mode: `ONLY_CRITICAL`

`ONLY_CRITICAL=true` scopes discovery/executions/governance down to only workflows tagged `CRITICAL_TAG` (default `criticidad:alta`) — used to onboard one workflow at a time instead of all 341 in the source n8n instance at once. When on, `executions.py` also skips `get_execution()` entirely (not just the per-node breakdown) for non-critical workflows' executions, which meaningfully cuts n8n API load. Governance is scoped the same way when this is on — meaning it stops being useful for its original purpose (finding *untagged* workflows), since anything untagged is excluded from the audit by definition. Turn `ONLY_CRITICAL` off (or remove it) once ready to monitor everything.

### Per-workflow SLO via tag, not a single host macro

A Zabbix host-level macro (`{$N8N.DURATION.SLO.MS}`, the original design) applies the same threshold to every workflow LLD-discovered on that host — doesn't work once workflows have genuinely different expected durations (a Wait-heavy call-escalation workflow legitimately takes minutes; most others should finish in seconds). Fixed by resolving the SLO in code instead of in Zabbix: `discovery.py` reads a `slo:<seconds>` tag per workflow (`discovery.slo_ms()`), falls back to `DEFAULT_SLO_MS` (env var, default 60000) when absent, and publishes it as an LLD macro `{#SLO_MS}` per discovered workflow. The template's SLO trigger prototype uses `{#SLO_MS}` (per-instance) instead of the old host macro, which was removed from `zabbix/n8n_template.yaml`.

## Commands

All commands run from `bridge/`:

```bash
cp .env.example .env    # then fill N8N_BASE_URL / N8N_API_KEY, keep DRY_RUN=true
npm start                # node src/index.js (requires Node >= 18 for native fetch)
```

There is no build step, test suite, or linter configured (`package.json` has no dependencies and only a `start` script). There is no `npm test` — validate behavior by running with `DRY_RUN=true` and reading stdout.

Docker:
```bash
docker build -t n8n-zabbix-bridge .
docker run --env-file .env -v n8n-zabbix-bridge-state:/data n8n-zabbix-bridge
```

## Architecture (bridge/, Node — reference only, not deployed)

Everything lives in `bridge/src/`, wired together by `index.js`, which runs three independent polling loops on their own intervals (`DISCOVERY_INTERVAL_MS`, `EXECUTIONS_INTERVAL_MS`, `GOVERNANCE_INTERVAL_MS`), each firing once immediately then on a timer. Each tick is independently try/caught so one loop's failure doesn't kill the others.

This describes the original Node design. **It has not received the fixes made to `bridge-python/`** (checkpoint clobbering `criticalWorkflowIds`, non-terminal/`waiting` executions, duration computed from node timestamps instead of `stoppedAt`, per-workflow SLO). If this ever gets deployed for real, port those fixes over first.

- **`n8nClient.js`** — minimal wrapper over n8n's public REST API (`/api/v1`) using native `fetch`. No SDK, no retry logic.
- **`discovery.js`** — polls `/workflows`, emits a Zabbix Low-Level Discovery (LLD) JSON payload (key `n8n.workflow.discovery`). This is what makes "every new workflow gets monitored automatically" work: the Zabbix template's discovery rule (`zabbix/n8n_template.yaml`) turns each discovered `{#WORKFLOW_ID}` into concrete items/triggers with zero manual config. `index.js` also uses this tick's output to recompute the set of "critical" workflow IDs (tagged `criticidad:alta` by default, see `CRITICAL_TAG`) used by `executions.js`.
- **`executions.js`** — polls `/executions`, diffs against a checkpoint (see below), and for each new execution extracts total duration and per-node timing from `resultData.runData`. Sends `n8n.workflow.duration.last`, `n8n.workflow.status.last`, `n8n.workflow.nodes.timing` (JSON blob) for every workflow, plus per-node historical items (`n8n.node.duration[workflow_id,node_name]`) *only* for workflows tagged critical — deliberate, to control Zabbix NVPS (item volume). See plan §8 for the tradeoff. Since node names aren't known ahead of time (unlike workflows, they only appear once an execution runs), `n8n.node.duration` items are populated via a second LLD rule (`n8n.node.discovery`, key emits `{#WORKFLOW_ID}`/`{#NODE_NAME}` pairs) fed from `state.criticalNodes`, a registry of node names seen per critical workflow that's persisted in `STATE_FILE` and re-sent in full on every tick — not just newly-seen nodes — so Zabbix doesn't mark already-discovered node items as "lost" during ticks where a critical workflow has no new executions.
- **`governance.js`** — audits active workflows against the convention (Error Workflow assigned + `team`/`criticidad` tags) and publishes a compliance ratio + non-compliant list. This is the "quality" layer on top of discovery's unconditional coverage — a workflow that skips the convention is still monitored by discovery, just flagged here.
- **`zabbixSender.js`** — does NOT reimplement the Zabbix trapper wire protocol. It writes a temp file in the `-i file -T` format and shells out to the real `zabbix_sender` binary (path via `ZABBIX_SENDER_BIN`), the same mechanism already used in production for other metrics. When `DRY_RUN=true`, it short-circuits and only logs the payload — never touches the temp file/binary path.

### State and checkpointing

`executions.js` persists `{ lastExecutionId }` to `STATE_FILE` (default `./state.json`, set to `/data/state.json` in Docker via a volume). On first run (no checkpoint), it only processes the first page of executions (most recent) to avoid backfilling the entire instance history; subsequent runs proceed incrementally from the checkpoint. Execution ordering is assumed descending (n8n default) — if pagination behavior differs in practice, this assumption needs revisiting.

### Zabbix template (`zabbix/n8n_template.yaml`)

Shared by both implementations — not Node-specific. Zabbix 7.x-compatible template YAML (trapper items + two LLD discovery rules + triggers), imported into production Zabbix and actively receiving data from `bridge-python`. Item/trigger keys here must stay in sync with the keys emitted by `discovery.py`/`executions.py`/`governance.py` (or the `.js` equivalents, if those are ever brought back in sync). Key macros: `{$N8N.HEARTBEAT.WINDOW}`, `{$N8N.GOVERNANCE.MIN_RATIO}` (host-level, same for every workflow); the duration SLO is **not** a host macro — see "Per-workflow SLO via tag" above, it's `{#SLO_MS}`, an LLD macro resolved per workflow.

Despite the file being written by hand originally "without network access to validate," this has since been imported into a real Zabbix (7.x — the exporter format declares `version: '6.0'` for schema compatibility, but the running server is newer, confirmed by fields like `enabled_lifetime_type` that Zabbix's importer adds on its own) and is live-receiving real production n8n data. If re-importing after edits: entities are matched by `uuid`, must be real UUIDv4 (Zabbix validates the version/variant nibbles, not just 32 hex chars) or import fails with `UUIDv4 is expected`.

### Caveats still open

1. **Node names containing `,`, `[`, or `]`** in `n8n.node.duration[workflow_id,node_name]` — the key is built via plain string interpolation in `executions.py`, which does not escape those characters, while Zabbix's own LLD macro substitution (used to materialize the item from `n8n.node.discovery`'s `{#NODE_NAME}`) does. A node name with a comma would produce a trapper key that doesn't match the item Zabbix actually created, and the value would be silently dropped. Not yet hit in practice (no real node name has needed it); revisit if one shows up.
2. **Zabbix Sender protocol framing** on very old Zabbix versions — implemented against the documented spec only, never tested against anything pre-5.x.
3. **`bridge/` (Node)** is unverified in the ways described in its own section above — everything in `bridge-python/` has been validated against the real n8n/Zabbix production instances (see git history / commit messages for what was found and fixed each time).

## Conventions

- No external dependencies by design, in **both** implementations: `bridge/` uses Node's native `fetch`/`child_process.execFile` (empty `package.json` deps); `bridge-python/` uses only the standard library (`urllib.request`, `subprocess`, `json`) — no `requests`, even though the deployed Python runtime happens to have it available. Keep it that way unless there's a strong reason not to.
- `bridge/`: CommonJS (`require`/`module.exports`), not ESM. `bridge-python/`: plain functions/modules, no classes unless something actually needs state beyond what a module-level function argument covers.
- Source comments and docs are in Spanish (matches the team's working language); match that when editing existing files. This file (CLAUDE.md) is the exception, in English.
- `.env` loading is hand-rolled (`loadDotEnv` in `index.js` / `load_dotenv` in `monitor.py`) and never overwrites variables already set in the environment — important for the Docker `--env-file` path in `bridge/`, and for one-off manual overrides in `bridge-python/` (e.g. `DRY_RUN=false ./python/bin/python3 monitor.py --mode executions` for a single real-mode run without touching `.env`).
- Always gate real Zabbix writes behind `DRY_RUN` when testing changes — flip to `false` only after confirming payloads look right in dry-run logs. For `bridge-python/`, prefer a one-off `DRY_RUN=false` env override over editing `.env` when just testing.
- `bridge-python/` deploy target has no `git`/`npm`/`docker` — changes get `scp`'d to `/opt/bmc/ETLs/n8n/monworkflows/` on `10.203.25.154` by hand. There's no CI here; "tested" means "ran on that box against real n8n/Zabbix," not "unit tested."
