#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point del monitoreo de n8n hacia Zabbix.

Puerto a Python de bridge/src/index.js, adaptado a invocacion por cron (una
pasada por corrida, seleccionable con --mode) en vez de un daemon con
setInterval -- este servidor no corre procesos Node persistentes, sigue la
convencion de /opt/bmc/ETLs (scripts disparados por cron). Ver CLAUDE.md.

Como cada modo corre en un proceso separado (a diferencia del daemon, que
compartia `criticalWorkflowIds` en memoria entre ticks), discovery persiste
el set de workflows criticos en STATE_FILE y executions lo lee de ahi.
"""

import argparse
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capacity  # noqa: E402
import discovery  # noqa: E402
import executions  # noqa: E402
import governance  # noqa: E402


def load_dotenv(file_path=None):
    file_path = file_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(file_path):
        return
    with open(file_path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def load_config():
    required = ['N8N_BASE_URL', 'N8N_API_KEY', 'ZABBIX_SERVER_HOST']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError('Faltan variables de entorno: {}'.format(', '.join(missing)))
    return {
        'zabbixHost': os.environ.get('ZABBIX_HOST_NAME', 'n8n'),
        'zabbixConfig': {
            'host': os.environ['ZABBIX_SERVER_HOST'],
            'port': int(os.environ.get('ZABBIX_SERVER_PORT', '10051')),
            'binPath': os.environ.get('ZABBIX_SENDER_BIN', '/usr/bin/zabbix_sender'),
        },
        'criticalTag': os.environ.get('CRITICAL_TAG', 'criticidad:alta'),
        'onlyCritical': os.environ.get('ONLY_CRITICAL', 'false').strip().lower() == 'true',
        'defaultSloMs': int(os.environ.get('DEFAULT_SLO_MS', '60000')),
    }


def load_critical_ids():
    state = executions.load_state()
    return set(state.get('criticalWorkflowIds', []))


def save_critical_workflows(names_by_id):
    # names_by_id: { workflow_id (str): workflow_name (str) }. Se guardan
    # ambos -- el set de ids (que ya usaba executions.py para el filtro
    # ONLY_CRITICAL) y ahora tambien los nombres, para que el LLD de nodos
    # (n8n.node.discovery) pueda publicar {#WORKFLOW_NAME} y no solo el id
    # crudo en el nombre de los items en Zabbix.
    state = executions.load_state()
    state['criticalWorkflowIds'] = sorted(names_by_id.keys())
    state['criticalWorkflowNames'] = names_by_id
    executions.save_state(state)


def tick_discovery(config):
    try:
        only_tag = config['criticalTag'] if config['onlyCritical'] else None
        workflows = discovery.run_discovery(
            config['zabbixHost'], config['zabbixConfig'], only_tag, config['defaultSloMs'])
        critical_names = {
            str(wf['id']): wf.get('name') or str(wf['id'])
            for wf in workflows
            if any(t.get('name') == config['criticalTag'] for t in (wf.get('tags') or []))
        }
        save_critical_workflows(critical_names)
        critical_ids = set(critical_names.keys())
        print('[discovery] {} workflows ({} criticos){}'.format(
            len(workflows), len(critical_ids), ' [ONLY_CRITICAL]' if config['onlyCritical'] else ''))
        return critical_ids
    except Exception as e:
        print('[discovery] error: {}'.format(e), file=sys.stderr)
        return None


def tick_executions(config, critical_ids=None):
    try:
        if critical_ids is None:
            critical_ids = load_critical_ids()
        result = executions.process_executions(
            config['zabbixHost'], config['zabbixConfig'], critical_ids, config['onlyCritical'])
        print('[executions] {} ejecuciones nuevas procesadas'.format(result['processedCount']))
    except Exception as e:
        print('[executions] error: {}'.format(e), file=sys.stderr)


def tick_governance(config):
    try:
        only_tag = config['criticalTag'] if config['onlyCritical'] else None
        result = governance.run_governance_audit(config['zabbixHost'], config['zabbixConfig'], only_tag)
        print('[governance] {} workflows activos, {} no conformes (ratio={:.2f})'.format(
            result['activeCount'], result['nonCompliantCount'], result['ratio']))
    except Exception as e:
        print('[governance] error: {}'.format(e), file=sys.stderr)


def tick_capacity(config):
    try:
        result = capacity.process_capacity(config['zabbixHost'], config['zabbixConfig'])
        print('[capacity] {} ejecuciones ({} errores, {} workflows activos), '
              'duracion prom={}ms p95={}ms'.format(
                  result['total'], result['errors'], result['activeWorkflows'],
                  result['avgDurationMs'], result['p95DurationMs']))
    except Exception as e:
        print('[capacity] error: {}'.format(e), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description='Monitoreo de n8n hacia Zabbix (una pasada por invocacion).')
    parser.add_argument(
        '--mode', choices=['discovery', 'executions', 'governance', 'capacity', 'all'], default='all')
    args = parser.parse_args()

    load_dotenv()
    if os.environ.get('DRY_RUN') == 'true':
        print('=== DRY_RUN=true: no se va a escribir nada en Zabbix, solo se loguea ===')

    config = load_config()

    if args.mode == 'discovery':
        tick_discovery(config)
    elif args.mode == 'executions':
        tick_executions(config)
    elif args.mode == 'governance':
        tick_governance(config)
    elif args.mode == 'capacity':
        tick_capacity(config)
    else:
        critical_ids = tick_discovery(config)
        tick_executions(config, critical_ids)
        tick_governance(config)
        tick_capacity(config)


if __name__ == '__main__':
    main()
