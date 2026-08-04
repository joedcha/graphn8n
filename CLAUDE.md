# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`n8n-zabbix-bridge`: a small Node.js service that connects n8n (Community Edition) to Zabbix so every workflow is automatically monitored, with per-node execution timing and governance auditing. Grafana visualizes the data via the Zabbix datasource — Zabbix does all collection/alerting, Grafana only renders dashboards. Full design rationale, architecture diagram, and roadmap live in `docs/plan-observabilidad-n8n-grafana.md` — read it before making architectural changes.

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

## Architecture

Everything lives in `bridge/src/`, wired together by `index.js`, which runs three independent polling loops on their own intervals (`DISCOVERY_INTERVAL_MS`, `EXECUTIONS_INTERVAL_MS`, `GOVERNANCE_INTERVAL_MS`), each firing once immediately then on a timer. Each tick is independently try/caught so one loop's failure doesn't kill the others.

- **`n8nClient.js`** — minimal wrapper over n8n's public REST API (`/api/v1`) using native `fetch`. No SDK, no retry logic.
- **`discovery.js`** — polls `/workflows`, emits a Zabbix Low-Level Discovery (LLD) JSON payload (key `n8n.workflow.discovery`). This is what makes "every new workflow gets monitored automatically" work: the Zabbix template's discovery rule (`zabbix/n8n_template.yaml`) turns each discovered `{#WORKFLOW_ID}` into concrete items/triggers with zero manual config. `index.js` also uses this tick's output to recompute the set of "critical" workflow IDs (tagged `criticidad:alta` by default, see `CRITICAL_TAG`) used by `executions.js`.
- **`executions.js`** — polls `/executions`, diffs against a checkpoint (see below), and for each new execution extracts total duration and per-node timing from `resultData.runData`. Sends `n8n.workflow.duration.last`, `n8n.workflow.status.last`, `n8n.workflow.nodes.timing` (JSON blob) for every workflow, plus per-node historical items (`n8n.node.duration[workflow_id,node_name]`) *only* for workflows tagged critical — deliberate, to control Zabbix NVPS (item volume). See plan §8 for the tradeoff.
- **`governance.js`** — audits active workflows against the convention (Error Workflow assigned + `team`/`criticidad` tags) and publishes a compliance ratio + non-compliant list. This is the "quality" layer on top of discovery's unconditional coverage — a workflow that skips the convention is still monitored by discovery, just flagged here.
- **`zabbixSender.js`** — does NOT reimplement the Zabbix trapper wire protocol. It writes a temp file in the `-i file -T` format and shells out to the real `zabbix_sender` binary (path via `ZABBIX_SENDER_BIN`), the same mechanism already used in production for other metrics. When `DRY_RUN=true`, it short-circuits and only logs the payload — never touches the temp file/binary path.

### State and checkpointing

`executions.js` persists `{ lastExecutionId }` to `STATE_FILE` (default `./state.json`, set to `/data/state.json` in Docker via a volume). On first run (no checkpoint), it only processes the first page of executions (most recent) to avoid backfilling the entire instance history; subsequent runs proceed incrementally from the checkpoint. Execution ordering is assumed descending (n8n default) — if pagination behavior differs in practice, this assumption needs revisiting.

### Zabbix template (`zabbix/n8n_template.yaml`)

Hand-written Zabbix 6.0 template YAML (trapper items + LLD discovery rule + triggers). Item/trigger keys here must stay in sync with the keys emitted by `discovery.js`/`executions.js`/`governance.js`. Key macros: `{$N8N.DURATION.SLO.MS}`, `{$N8N.HEARTBEAT.WINDOW}`, `{$N8N.GOVERNANCE.MIN_RATIO}`.

### Important caveat: unverified against real infrastructure

This code was written without network access to a real n8n or Zabbix instance (see `bridge/README.md` "Estado de este código" and comments in `n8nClient.js`/`executions.js`/`zabbix/n8n_template.yaml`). Two things specifically need validation against the real systems before being trusted:

1. **Where `runData` actually lives** in the response of `GET /api/v1/executions/{id}?includeData=true` — it may differ by n8n version. `extractNodeTimings` in `executions.js` tries several known paths and logs a warning with the received top-level keys if none match; use that log to adjust the candidate paths.
2. **Zabbix Sender protocol framing** on very old Zabbix versions — implemented against the documented spec only.

When touching `executions.js` or the n8n client, keep this validate-before-trust posture rather than assuming the parsing is correct.

## Conventions

- No external npm dependencies by design (`package.json` deps are empty) — uses Node's native `fetch` and `child_process.execFile`. Keep it that way unless there's a strong reason not to.
- CommonJS (`require`/`module.exports`), not ESM.
- Source comments and docs are in Spanish (matches the team's working language); match that when editing existing files.
- `.env` loading is hand-rolled in `index.js` (`loadDotEnv`) and never overwrites variables already set in the environment (important for the Docker `--env-file` path).
- Always gate real Zabbix writes behind `DRY_RUN` when testing changes — flip to `false` only after confirming payloads look right in dry-run logs.
