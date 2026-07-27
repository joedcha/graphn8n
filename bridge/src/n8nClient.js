'use strict';

// Cliente minimo contra la API publica de n8n (/api/v1).
// Requiere Node >= 18 (usa fetch nativo).
//
// NOTA: no se pudo validar contra una instancia real de n8n desde este
// entorno (sin acceso de red a hosts internos). Los nombres de endpoint y
// campos siguen la documentacion publica de n8n; validar con una llamada
// real (ver README, seccion "Primer chequeo") antes de confiar en la logica
// de parseo de executions.js.

function getConfig() {
  const baseUrl = process.env.N8N_BASE_URL;
  const apiKey = process.env.N8N_API_KEY;
  if (!baseUrl || !apiKey) {
    throw new Error('Faltan N8N_BASE_URL y/o N8N_API_KEY en el entorno');
  }
  return { baseUrl: baseUrl.replace(/\/+$/, ''), apiKey };
}

async function n8nGet(path, params = {}) {
  const { baseUrl, apiKey } = getConfig();
  const url = new URL(`${baseUrl}/api/v1${path}`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }

  const res = await fetch(url, {
    headers: { 'X-N8N-API-KEY': apiKey, Accept: 'application/json' },
  });

  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`n8n API ${path} -> HTTP ${res.status}: ${body.slice(0, 200)}`);
  }

  return res.json();
}

async function listWorkflows() {
  const results = [];
  let cursor;
  do {
    const page = await n8nGet('/workflows', { limit: 100, cursor });
    results.push(...(page.data || []));
    cursor = page.nextCursor || undefined;
  } while (cursor);
  return results;
}

// Devuelve ejecuciones terminadas (exito o error), sin el detalle por nodo.
async function listExecutions({ cursor } = {}) {
  return n8nGet('/executions', { limit: 50, cursor, includeData: false });
}

// Detalle completo, incluyendo resultData.runData con tiempos por nodo.
async function getExecution(id) {
  return n8nGet(`/executions/${id}`, { includeData: true });
}

module.exports = { listWorkflows, listExecutions, getExecution };
