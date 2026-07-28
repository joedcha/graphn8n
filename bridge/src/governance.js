'use strict';

const { listWorkflows } = require('./n8nClient');
const { sendToZabbix } = require('./zabbixSender');
const { tagValue } = require('./discovery');

function isCompliant(workflow) {
  const hasErrorWorkflow = Boolean(workflow.settings && workflow.settings.errorWorkflow);
  const hasTeamTag = Boolean(tagValue(workflow.tags, 'team'));
  const hasCriticalityTag = Boolean(tagValue(workflow.tags, 'criticidad'));
  return hasErrorWorkflow && hasTeamTag && hasCriticalityTag;
}

// Audita todos los workflows activos contra la convencion definida en el
// plan (Error Workflow asignado + tags team/criticidad) y publica el
// porcentaje de cumplimiento. Esta es la capa "de calidad"; la cobertura
// minima ya la garantiza el discovery (discovery.js) sin depender de esto.
async function runGovernanceAudit({ zabbixHost, zabbixConfig }) {
  const workflows = await listWorkflows();
  const active = workflows.filter((wf) => wf.active);
  const nonCompliant = active.filter((wf) => !isCompliant(wf));
  const ratio = active.length ? (active.length - nonCompliant.length) / active.length : 1;

  const items = [
    {
      host: zabbixHost,
      key: 'n8n.governance.compliance_ratio',
      value: Number(ratio.toFixed(4)),
    },
    {
      host: zabbixHost,
      key: 'n8n.governance.non_compliant',
      value: JSON.stringify(
        nonCompliant.map((wf) => ({
          id: wf.id,
          name: wf.name,
          hasErrorWorkflow: Boolean(wf.settings && wf.settings.errorWorkflow),
          hasTeamTag: Boolean(tagValue(wf.tags, 'team')),
          hasCriticalityTag: Boolean(tagValue(wf.tags, 'criticidad')),
        }))
      ),
    },
  ];

  await sendToZabbix({
    host: zabbixConfig.host,
    port: zabbixConfig.port,
    binPath: zabbixConfig.binPath,
    items,
  });

  return { activeCount: active.length, nonCompliantCount: nonCompliant.length, ratio };
}

module.exports = { runGovernanceAudit, isCompliant };
