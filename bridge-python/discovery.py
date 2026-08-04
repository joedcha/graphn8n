# -*- coding: utf-8 -*-
"""Puerto 1:1 de bridge/src/discovery.js."""

import json

from n8n_client import list_workflows
from zabbix_sender import send_to_zabbix


def tag_value(tags, prefix):
    needle = prefix + ':'
    for t in (tags or []):
        name = t.get('name') or ''
        if name.startswith(needle):
            return name[len(needle):]
    return None


def slo_ms(tags, default_slo_ms):
    """SLO de duracion del workflow, en ms. Se toma del tag `slo:<segundos>`
    en n8n (ej. 'slo:600' = 10 minutos); si el workflow no lo tiene, se usa
    el default (DEFAULT_SLO_MS). Cada workflow puede tener uno distinto --
    se resuelve aca (no en Zabbix) porque un macro de host aplicaria el
    mismo umbral a todos los workflows descubiertos en ese host.
    """
    raw = tag_value(tags, 'slo')
    if raw is None:
        return default_slo_ms
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return default_slo_ms


def run_discovery(zabbix_host, zabbix_config, only_critical_tag=None, default_slo_ms=60000):
    workflows = list_workflows()

    if only_critical_tag:
        # Modo piloto: solo publicar (y por lo tanto solo dejar que Zabbix
        # cree items/triggers para) los workflows tageados como criticos, en
        # vez de los 341 workflows del n8n de produccion. Ver CLAUDE.md.
        workflows = [
            wf for wf in workflows
            if any(t.get('name') == only_critical_tag for t in (wf.get('tags') or []))
        ]

    data = [
        {
            '{#WORKFLOW_ID}': str(wf.get('id')),
            '{#WORKFLOW_NAME}': wf.get('name'),
            '{#TEAM}': tag_value(wf.get('tags'), 'team') or 'sin-equipo',
            '{#CRITICALITY}': tag_value(wf.get('tags'), 'criticidad') or 'media',
            '{#SLO_MS}': str(slo_ms(wf.get('tags'), default_slo_ms)),
        }
        for wf in workflows
    ]

    send_to_zabbix(
        zabbix_config['host'],
        zabbix_config['port'],
        zabbix_config['binPath'],
        [{
            'host': zabbix_host,
            'key': 'n8n.workflow.discovery',
            'value': json.dumps({'data': data}, ensure_ascii=False),
        }],
    )

    return workflows
