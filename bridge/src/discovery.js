'use strict';

const { listWorkflows } = require('./n8nClient');
const { sendToZabbix } = require('./zabbixSender');

function tagValue(tags, prefix) {
  const tag = (tags || []).find((t) => (t.name || '').startsWith(`${prefix}:`));
  return tag ? tag.name.slice(prefix.length + 1) : null;
}

// Genera el JSON de Low-Level Discovery que espera Zabbix y lo envia como
// item trapper. La regla de discovery en Zabbix (ver zabbix/n8n_template.yaml)
// crea automaticamente los items/triggers por cada workflow detectado aqui.
async function runDiscovery({ zabbixHost, zabbixConfig }) {
  const workflows = await listWorkflows();

  const lld = {
    data: workflows.map((wf) => ({
      '{#WORKFLOW_ID}': String(wf.id),
      '{#WORKFLOW_NAME}': wf.name,
      '{#TEAM}': tagValue(wf.tags, 'team') || 'sin-equipo',
      '{#CRITICALITY}': tagValue(wf.tags, 'criticidad') || 'media',
    })),
  };

  await sendToZabbix({
    host: zabbixConfig.host,
    port: zabbixConfig.port,
    binPath: zabbixConfig.binPath,
    items: [
      {
        host: zabbixHost,
        key: 'n8n.workflow.discovery',
        value: JSON.stringify(lld),
      },
    ],
  });

  return workflows;
}

module.exports = { runDiscovery, tagValue };
