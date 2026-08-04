# -*- coding: utf-8 -*-
"""Puerto 1:1 de bridge/src/governance.js."""

import json

from discovery import tag_value
from n8n_client import list_workflows
from zabbix_sender import send_to_zabbix


def is_compliant(workflow):
    settings = workflow.get('settings') or {}
    has_error_workflow = bool(settings.get('errorWorkflow'))
    has_team_tag = bool(tag_value(workflow.get('tags'), 'team'))
    has_criticality_tag = bool(tag_value(workflow.get('tags'), 'criticidad'))
    return has_error_workflow and has_team_tag and has_criticality_tag


def run_governance_audit(zabbix_host, zabbix_config, only_critical_tag=None):
    workflows = list_workflows()
    if only_critical_tag:
        # Modo piloto: auditar solo el/los workflow(s) criticos. Ojo -- esto
        # renuncia al proposito original de gobernanza (detectar workflows
        # que ni siquiera estan tageados), ver CLAUDE.md.
        workflows = [
            wf for wf in workflows
            if any(t.get('name') == only_critical_tag for t in (wf.get('tags') or []))
        ]
    active = [wf for wf in workflows if wf.get('active')]
    non_compliant = [wf for wf in active if not is_compliant(wf)]
    ratio = (len(active) - len(non_compliant)) / len(active) if active else 1

    items = [
        {
            'host': zabbix_host,
            'key': 'n8n.governance.compliance_ratio',
            'value': round(ratio, 4),
        },
        {
            'host': zabbix_host,
            'key': 'n8n.governance.non_compliant',
            'value': json.dumps([
                {
                    'id': wf.get('id'),
                    'name': wf.get('name'),
                    'hasErrorWorkflow': bool((wf.get('settings') or {}).get('errorWorkflow')),
                    'hasTeamTag': bool(tag_value(wf.get('tags'), 'team')),
                    'hasCriticalityTag': bool(tag_value(wf.get('tags'), 'criticidad')),
                }
                for wf in non_compliant
            ], ensure_ascii=False),
        },
    ]

    send_to_zabbix(zabbix_config['host'], zabbix_config['port'], zabbix_config['binPath'], items)

    return {'activeCount': len(active), 'nonCompliantCount': len(non_compliant), 'ratio': ratio}
