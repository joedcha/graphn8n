# -*- coding: utf-8 -*-
"""Metricas de capacidad AGREGADAS sobre TODOS los workflows de n8n (no solo
los criticos/tageados que monitorea executions.py).

Deliberadamente separado de executions.py:
- executions.py hace tracking detallado (duracion real por nodo, tiempos por
  workflow) pero solo de los workflows tageados como criticos -- eso es a
  proposito, para no generar cientos de items por workflow en Zabbix (ver
  CLAUDE.md, "Pilot mode: ONLY_CRITICAL").
- capacity.py necesita ver TODA la actividad de n8n para poder responder
  "cuanto volumen soporta la instancia hoy", pero como eso son cientos de
  workflows, solo manda un puñado de metricas AGREGADAS (no una por
  workflow) -- por eso no explota el volumen de items aunque cubra los 341
  workflows en vez de 1 o 2.
- Por el mismo motivo de costo, usa solo el resumen de /executions (nunca
  pide el detalle por ejecucion via get_execution) -- eso significa que la
  duracion se calcula con stoppedAt-startedAt "crudo", no con la
  reconstruccion via runData que hace executions.compute_duration_ms. Para
  workflows con nodos Wait esto puede subestimar la duracion real de ESE
  workflow puntual, pero para una foto agregada de "cuanto tarda todo hoy"
  es una aproximacion aceptable -- si se necesita la duracion exacta de un
  workflow puntual, ya esta cubierta por executions.py si esta tageado.
"""

import json
import os
import statistics

from n8n_client import list_executions, list_workflows
from zabbix_sender import send_to_zabbix

# Mismo criterio que executions.py: no marcar como "vistas" ejecuciones que
# todavia estan en curso, para no perder su resultado final ni contarlas
# antes de tiempo. Ver executions.py para el porque (n8n muta startedAt/
# stoppedAt mientras esta pausada en un nodo Wait).
NON_TERMINAL_STATUSES = {'waiting', 'running', 'new'}


def _state_file():
    return os.environ.get('CAPACITY_STATE_FILE', './capacity_state.json')


def load_state():
    try:
        with open(_state_file(), 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    return {'lastExecutionId': None, **state}


def save_state(state):
    with open(_state_file(), 'w', encoding='utf-8') as f:
        json.dump(state, f)


def _duration_ms(execution_summary):
    started, stopped = execution_summary.get('startedAt'), execution_summary.get('stoppedAt')
    if not started or not stopped:
        return None
    try:
        from datetime import datetime
        d0 = datetime.fromisoformat(started.replace('Z', '+00:00'))
        d1 = datetime.fromisoformat(stopped.replace('Z', '+00:00'))
        ms = (d1 - d0).total_seconds() * 1000
        return ms if ms >= 0 else None
    except (ValueError, TypeError):
        return None


def _percentile(values, pct):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] + (values[c] - values[f]) * (k - f)


def process_capacity(zabbix_host, zabbix_config):
    state = load_state()
    last_id = int(state['lastExecutionId']) if state['lastExecutionId'] else None

    cursor = None
    newest_id = last_id
    min_pending_id = None

    total = 0
    errors = 0
    durations = []
    workflow_ids = set()

    while True:
        page = list_executions(cursor=cursor)
        batch = page.get('data') or []
        if not batch:
            break

        for summary in batch:
            exec_id = int(summary['id'])
            if last_id and exec_id <= last_id:
                continue

            if summary.get('status') in NON_TERMINAL_STATUSES:
                if min_pending_id is None or exec_id < min_pending_id:
                    min_pending_id = exec_id
                continue

            total += 1
            if summary.get('status') in ('error', 'crashed'):
                errors += 1
            dur = _duration_ms(summary)
            if dur is not None:
                durations.append(dur)
            workflow_ids.add(summary.get('workflowId'))

            if not newest_id or exec_id > newest_id:
                newest_id = exec_id

        cursor = page.get('nextCursor') or None

        if not last_id:
            # Primera corrida: solo la pagina mas reciente, mismo criterio
            # que executions.py (no traer todo el historico de una).
            break
        if any(int(e['id']) <= last_id for e in batch):
            break
        if not cursor:
            break

    if min_pending_id is not None and (newest_id is None or newest_id >= min_pending_id):
        newest_id = min_pending_id - 1

    # Inventario: cuantos workflows existen en total, corran o no en esta
    # ventana. `active_workflows` arriba solo cuenta los que SI tuvieron
    # ejecuciones en estos 5 min -- esto es el universo completo, para poder
    # ver crecimiento del inventario en el tiempo (341 hoy, cuanto en 3 meses).
    all_workflows = list_workflows()
    workflows_total = len(all_workflows)
    active_workflows_list = sorted(
        [wf for wf in all_workflows if wf.get('active')], key=lambda wf: (wf.get('name') or '').lower()
    )
    inactive_workflows_list = sorted(
        [wf for wf in all_workflows if not wf.get('active')], key=lambda wf: (wf.get('name') or '').lower()
    )
    workflows_active = len(active_workflows_list)
    workflows_inactive = len(inactive_workflows_list)

    def _names_json(workflows):
        return json.dumps(
            [{'id': wf.get('id'), 'name': wf.get('name')} for wf in workflows], ensure_ascii=False
        )

    items = [
        {'host': zabbix_host, 'key': 'n8n.capacity.workflows.total', 'value': workflows_total},
        {'host': zabbix_host, 'key': 'n8n.capacity.workflows.active', 'value': workflows_active},
        {'host': zabbix_host, 'key': 'n8n.capacity.workflows.inactive', 'value': workflows_inactive},
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.workflows.active_names',
            'value': _names_json(active_workflows_list),
        },
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.workflows.inactive_names',
            'value': _names_json(inactive_workflows_list),
        },
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.workflows.all_names',
            'value': json.dumps(
                [
                    {'id': wf.get('id'), 'name': wf.get('name'), 'active': bool(wf.get('active'))}
                    for wf in sorted(all_workflows, key=lambda wf: (wf.get('name') or '').lower())
                ],
                ensure_ascii=False,
            ),
        },
        {'host': zabbix_host, 'key': 'n8n.capacity.executions.total', 'value': total},
        {'host': zabbix_host, 'key': 'n8n.capacity.executions.errors', 'value': errors},
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.executions.error_rate',
            'value': round(errors / total * 100, 2) if total else 0,
        },
        {'host': zabbix_host, 'key': 'n8n.capacity.executions.active_workflows', 'value': len(workflow_ids)},
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.duration.avg_ms',
            'value': round(statistics.mean(durations)) if durations else 0,
        },
        {
            'host': zabbix_host,
            'key': 'n8n.capacity.duration.p95_ms',
            'value': round(_percentile(durations, 95)) if durations else 0,
        },
    ]

    # A diferencia de antes, ahora siempre hay algo que mandar (el inventario
    # de workflows se pide en cada corrida, haya habido ejecuciones nuevas o
    # no) -- ya no hace falta condicionar el envio a `total`.
    send_to_zabbix(zabbix_config['host'], zabbix_config['port'], zabbix_config['binPath'], items)

    if newest_id and newest_id != last_id:
        save_state({'lastExecutionId': newest_id})

    return {
        'total': total,
        'errors': errors,
        'activeWorkflows': len(workflow_ids),
        'avgDurationMs': round(statistics.mean(durations)) if durations else 0,
        'p95DurationMs': round(_percentile(durations, 95)) if durations else 0,
    }
