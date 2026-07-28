'use strict';

const fs = require('fs');

const { listExecutions, getExecution } = require('./n8nClient');
const { sendToZabbix } = require('./zabbixSender');

function stateFile() {
  return process.env.STATE_FILE || './state.json';
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
  } catch {
    return { lastExecutionId: null };
  }
}

function saveState(state) {
  fs.writeFileSync(stateFile(), JSON.stringify(state));
}

// Extrae, por nodo, cuanto tardo la ultima corrida de ese nodo dentro de la
// ejecucion. runData tiene la forma { [nodeName]: [ { startTime, executionTime,
// executionStatus, error, ... }, ... ] } segun la estructura interna de n8n.
//
// OJO: no se pudo confirmar contra una respuesta real de n8n si la API
// publica (/api/v1/executions/{id}?includeData=true) devuelve el campo bajo
// `data.resultData.runData` (como el motor interno) o con otro anidado.
// Por eso se prueban varias rutas conocidas; si ninguna matchea, se loguea
// un warning con las claves top-level recibidas para poder ajustar esto
// rapido contra la instancia real.
function extractNodeTimings(execution) {
  const candidates = [
    execution?.data?.resultData?.runData,
    execution?.data?.executionData?.resultData?.runData,
    execution?.resultData?.runData,
  ];
  const runData = candidates.find((c) => c && typeof c === 'object');

  if (!runData) {
    console.warn(
      `[executions] no se encontro runData en la ejecucion ${execution?.id}. ` +
        `Claves recibidas en execution.data: ${Object.keys(execution?.data || {}).join(', ') || '(vacio)'}`
    );
    return null;
  }

  return Object.entries(runData).map(([nodeName, runs]) => {
    const lastRun = Array.isArray(runs) ? runs[runs.length - 1] : runs;
    const hasError = Boolean(lastRun?.error);
    return {
      node: nodeName,
      ms: typeof lastRun?.executionTime === 'number' ? lastRun.executionTime : null,
      status: hasError ? 'error' : 'success',
      errorMessage: hasError ? String(lastRun.error.message || lastRun.error) : null,
    };
  });
}

function computeDurationMs(execution) {
  if (!execution.startedAt || !execution.stoppedAt) return null;
  const ms = new Date(execution.stoppedAt).getTime() - new Date(execution.startedAt).getTime();
  return Number.isFinite(ms) ? ms : null;
}

function isFailed(execution) {
  if (execution.status) return execution.status === 'error' || execution.status === 'crashed';
  return execution.finished === false;
}

async function processExecutions({ zabbixHost, zabbixConfig, criticalWorkflowIds }) {
  const state = loadState();
  const lastId = state.lastExecutionId ? Number(state.lastExecutionId) : null;

  let cursor;
  let newestId = lastId;
  const items = [];
  let processedCount = 0;

  do {
    const page = await listExecutions({ cursor });
    const batch = page.data || [];

    // Se asume orden descendente (mas reciente primero), que es el default
    // de n8n. Si en la practica viene ascendente, invertir este corte.
    for (const summary of batch) {
      const execId = Number(summary.id);
      if (lastId && execId <= lastId) continue;

      const full = await getExecution(summary.id);
      const durationMs = computeDurationMs(full);
      const failed = isFailed(full);
      const nodeTimings = extractNodeTimings(full);

      items.push({
        host: zabbixHost,
        key: `n8n.workflow.duration.last[${full.workflowId}]`,
        value: durationMs ?? 0,
      });
      items.push({
        host: zabbixHost,
        key: `n8n.workflow.status.last[${full.workflowId}]`,
        value: failed ? 1 : 0,
      });

      if (nodeTimings) {
        items.push({
          host: zabbixHost,
          key: `n8n.workflow.nodes.timing[${full.workflowId}]`,
          value: JSON.stringify(nodeTimings),
        });

        if (criticalWorkflowIds.has(String(full.workflowId))) {
          for (const nt of nodeTimings) {
            items.push({
              host: zabbixHost,
              key: `n8n.node.duration[${full.workflowId},${nt.node}]`,
              value: nt.ms ?? 0,
            });
          }
        }
      }

      if (failed) {
        const failedNode = nodeTimings?.find((n) => n.status === 'error');
        items.push({
          host: zabbixHost,
          key: `n8n.workflow.error.last[${full.workflowId}]`,
          value: JSON.stringify({
            executionId: full.id,
            node: failedNode?.node ?? null,
            message: failedNode?.errorMessage ?? 'error sin detalle de nodo',
          }),
        });
      }

      if (!newestId || execId > newestId) newestId = execId;
      processedCount += 1;
    }

    cursor = page.nextCursor || undefined;

    if (!lastId) {
      // Primera corrida (sin checkpoint todavia): solo se procesa la
      // primera pagina (las ejecuciones mas recientes) para no traer todo
      // el historico de la instancia. A partir de la proxima corrida ya
      // queda un checkpoint y se sigue de forma incremental.
      break;
    }
    // Si ya llegamos a ejecuciones anteriores al checkpoint, no hace falta
    // seguir paginando hacia atras.
    if (batch.some((e) => Number(e.id) <= lastId)) break;
  } while (cursor);

  if (items.length) {
    await sendToZabbix({
      host: zabbixConfig.host,
      port: zabbixConfig.port,
      binPath: zabbixConfig.binPath,
      items,
    });
  }
  if (newestId && newestId !== lastId) {
    saveState({ lastExecutionId: newestId });
  }

  return { processedCount };
}

module.exports = { processExecutions, extractNodeTimings, computeDurationMs, isFailed };
