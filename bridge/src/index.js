'use strict';

const fs = require('fs');
const path = require('path');

const { runDiscovery } = require('./discovery');
const { processExecutions } = require('./executions');
const { runGovernanceAudit } = require('./governance');

// Carga simple de .env (sin dependencias npm): no pisa variables que ya
// esten seteadas en el entorno (por ejemplo, si se corre con docker --env-file).
function loadDotEnv(file = path.join(__dirname, '..', '.env')) {
  if (!fs.existsSync(file)) return;
  for (const rawLine of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadDotEnv();

function loadConfig() {
  const required = ['N8N_BASE_URL', 'N8N_API_KEY', 'ZABBIX_SERVER_HOST'];
  const missing = required.filter((k) => !process.env[k]);
  if (missing.length) {
    throw new Error(`Faltan variables de entorno: ${missing.join(', ')}`);
  }

  return {
    zabbixHost: process.env.ZABBIX_HOST_NAME || 'n8n',
    zabbixConfig: {
      host: process.env.ZABBIX_SERVER_HOST,
      port: Number(process.env.ZABBIX_SERVER_PORT || 10051),
      binPath: process.env.ZABBIX_SENDER_BIN || '/usr/bin/zabbix_sender',
    },
    criticalTag: process.env.CRITICAL_TAG || 'criticidad:alta',
    discoveryIntervalMs: Number(process.env.DISCOVERY_INTERVAL_MS || 5 * 60 * 1000),
    executionsIntervalMs: Number(process.env.EXECUTIONS_INTERVAL_MS || 45 * 1000),
    governanceIntervalMs: Number(process.env.GOVERNANCE_INTERVAL_MS || 60 * 60 * 1000),
  };
}

async function main() {
  const config = loadConfig();
  let criticalWorkflowIds = new Set();

  if (process.env.DRY_RUN === 'true') {
    console.log('=== DRY_RUN=true: no se va a escribir nada en Zabbix, solo se loguea ===');
  }

  async function tickDiscovery() {
    try {
      const workflows = await runDiscovery(config);
      criticalWorkflowIds = new Set(
        workflows
          .filter((wf) => (wf.tags || []).some((t) => t.name === config.criticalTag))
          .map((wf) => String(wf.id))
      );
      console.log(`[discovery] ${workflows.length} workflows (${criticalWorkflowIds.size} criticos)`);
    } catch (err) {
      console.error('[discovery] error:', err.message);
    }
  }

  async function tickExecutions() {
    try {
      const { processedCount } = await processExecutions({ ...config, criticalWorkflowIds });
      console.log(`[executions] ${processedCount} ejecuciones nuevas procesadas`);
    } catch (err) {
      console.error('[executions] error:', err.message);
    }
  }

  async function tickGovernance() {
    try {
      const { activeCount, nonCompliantCount, ratio } = await runGovernanceAudit(config);
      console.log(
        `[governance] ${activeCount} workflows activos, ${nonCompliantCount} no conformes (ratio=${ratio.toFixed(2)})`
      );
    } catch (err) {
      console.error('[governance] error:', err.message);
    }
  }

  // Primera corrida inmediata, despues por intervalo.
  await tickDiscovery();
  await tickExecutions();
  await tickGovernance();

  setInterval(tickDiscovery, config.discoveryIntervalMs);
  setInterval(tickExecutions, config.executionsIntervalMs);
  setInterval(tickGovernance, config.governanceIntervalMs);
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
