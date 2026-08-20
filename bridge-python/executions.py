# -*- coding: utf-8 -*-
"""Puerto 1:1 de bridge/src/executions.js (incluye el 2do nivel de LLD
n8n.node.discovery agregado para los items n8n.node.duration[...]).
"""

import json
import os
import sys

from n8n_client import get_execution, list_executions
from zabbix_sender import send_to_zabbix


def _state_file():
    return os.environ.get('STATE_FILE', './state.json')


def load_state():
    try:
        with open(_state_file(), 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    return {
        'lastExecutionId': None,
        'criticalNodes': {},
        'criticalWorkflowNames': {},
        'executionCounts': {},
        **state,
    }


# Cuantos IDs de ejecucion se recuerdan por workflow para no contarlos dos
# veces. executions.py reenvia (a proposito, ver process_executions) la
# misma ejecucion terminal en cada tick de cron mientras haya OTRA ejecucion
# pendiente del mismo workflow -- por eso un conteo ingenuo (values enviados
# a Zabbix) queda inflado por reenvios, no refleja ejecuciones reales. Este
# tope es solo para acotar el crecimiento del set en state.json; alcanza de
# sobra para deduplicar dentro de una sola ventana de pendiente (minutos),
# que es el unico escenario real de reenvio.
MAX_SEEN_IDS_PER_WORKFLOW = 500


def register_execution_count(state, workflow_id, exec_id, failed):
    """Suma 1 al contador acumulado de ejecuciones (y de errores, si
    `failed`) de `workflow_id` la PRIMERA vez que se ve `exec_id` -- ignora
    reenvios del mismo id en ticks posteriores. Devuelve (total, errors)
    ya actualizados, listos para mandar como items de Zabbix.
    """
    wf_id = str(workflow_id)
    counts = state['executionCounts'].setdefault(wf_id, {'total': 0, 'errors': 0, 'seenIds': []})
    seen = counts['seenIds']
    if exec_id not in seen:
        seen.append(exec_id)
        if len(seen) > MAX_SEEN_IDS_PER_WORKFLOW:
            del seen[:-MAX_SEEN_IDS_PER_WORKFLOW]
        counts['total'] += 1
        if failed:
            counts['errors'] += 1
    return counts['total'], counts['errors']


def save_state(state):
    with open(_state_file(), 'w', encoding='utf-8') as f:
        json.dump(state, f)


def _dig(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def extract_node_timings(execution):
    """Extrae, por nodo, cuanto tardo la ultima corrida de ese nodo dentro de
    la ejecucion. Prueba varias rutas conocidas de runData (ver comentario en
    executions.js original sobre por que esto no estaba validado de antemano).
    """
    run_data = _run_data(execution)

    if run_data is None:
        data_keys = ', '.join((execution.get('data') or {}).keys()) or '(vacio)'
        print(
            '[executions] no se encontro runData en la ejecucion {}. '
            'Claves recibidas en execution.data: {}'.format(execution.get('id'), data_keys),
            file=sys.stderr,
        )
        return None

    timings = []
    for node_name, runs in run_data.items():
        last_run = runs[-1] if isinstance(runs, list) and runs else (runs if isinstance(runs, dict) else {})
        has_error = bool(last_run.get('error'))
        ms = last_run.get('executionTime')
        error_obj = last_run.get('error')
        timings.append({
            'node': node_name,
            'ms': ms if isinstance(ms, (int, float)) else None,
            'status': 'error' if has_error else 'success',
            'errorMessage': str((error_obj or {}).get('message') or error_obj) if has_error else None,
        })
    return timings


def _run_data(execution):
    candidates = [
        _dig(execution, 'data', 'resultData', 'runData'),
        _dig(execution, 'data', 'executionData', 'resultData', 'runData'),
        _dig(execution, 'resultData', 'runData'),
    ]
    return next((c for c in candidates if isinstance(c, dict)), None)


def _duration_from_timestamps(execution):
    started, stopped = execution.get('startedAt'), execution.get('stoppedAt')
    if not started or not stopped:
        return None
    try:
        from datetime import datetime
        fmt_started = datetime.fromisoformat(started.replace('Z', '+00:00'))
        fmt_stopped = datetime.fromisoformat(stopped.replace('Z', '+00:00'))
        return int((fmt_stopped - fmt_started).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


def compute_duration_ms(execution):
    """Duracion total real de la ejecucion.

    OJO: en workflows con nodos Wait, `startedAt`/`stoppedAt` del objeto
    ejecucion NO reflejan el total -- n8n los pisa para reflejar solo el
    ultimo tramo desde la ultima reanudacion (confirmado empiricamente: la
    suma de tiempos de nodo, incluidas las pausas Wait, superaba por mucho
    el "stoppedAt - startedAt" reportado). Por eso se prioriza reconstruir
    el span real a partir de `startTime`/`executionTime` de cada nodo en
    runData (min inicio a max fin), y solo se cae a startedAt/stoppedAt si
    no hay runData disponible.

    Excepcion: si la ejecucion es un reintento (`retryOf` seteado), n8n
    reutiliza en su runData los nodos que ya habian corrido bien en el
    intento original -- con el `startTime` VIEJO de ese intento -- y solo
    trae startTime nuevo para los nodos re-ejecutados desde el punto de
    falla. La reconstruccion min/max de mas abajo no distingue esto: mezcla
    el inicio del intento original con el fin del reintento y devuelve una
    duracion inflada que no ocurrio asi en la realidad (confirmado en
    incidente real 2026-08-19: un reintento de 827ms se reporto como 74
    minutos). Para reintentos, `startedAt`/`stoppedAt` de la ejecucion SI
    son confiables (no hay nodos Wait de por medio en un reintento corto),
    asi que se usan directo, saltandose la reconstruccion por runData.
    """
    if execution.get('retryOf'):
        direct = _duration_from_timestamps(execution)
        if direct is not None:
            return direct

    run_data = _run_data(execution)
    if run_data:
        starts, ends = [], []
        for runs in run_data.values():
            run_list = runs if isinstance(runs, list) else [runs]
            for r in run_list:
                if not isinstance(r, dict):
                    continue
                start = r.get('startTime')
                exec_time = r.get('executionTime')
                if isinstance(start, (int, float)):
                    starts.append(start)
                    if isinstance(exec_time, (int, float)):
                        ends.append(start + exec_time)
        if starts and ends:
            return int(max(ends) - min(starts))

    return _duration_from_timestamps(execution)


def is_failed(execution):
    status = execution.get('status')
    if status:
        return status in ('error', 'crashed')
    return execution.get('finished') is False


# Ejecuciones que todavia no terminaron (ej. pausadas en un nodo Wait de
# n8n). OJO: 'finished' no sirve para distinguir esto -- n8n lo pone en
# False tanto para ejecuciones en curso como para ejecuciones ya
# terminadas en error, asi que hay que guiarse por 'status'.
NON_TERMINAL_STATUSES = {'waiting', 'running', 'new'}


def process_executions(zabbix_host, zabbix_config, critical_workflow_ids, only_critical=False):
    state = load_state()
    last_id = int(state['lastExecutionId']) if state['lastExecutionId'] else None

    cursor = None
    newest_id = last_id
    items = []
    processed_count = 0
    min_pending_id = None

    while True:
        page = list_executions(cursor=cursor)
        batch = page.get('data') or []

        for summary in batch:
            exec_id = int(summary['id'])
            if last_id and exec_id <= last_id:
                continue

            if summary.get('status') in NON_TERMINAL_STATUSES:
                # Todavia no termino (ej. pausada en un Wait). No se marca
                # como vista: hay que evitar que el checkpoint avance mas
                # alla de este id, para volver a pedirla en la proxima
                # corrida cuando ya haya terminado de verdad.
                if min_pending_id is None or exec_id < min_pending_id:
                    min_pending_id = exec_id
                continue

            if only_critical and str(summary.get('workflowId')) not in critical_workflow_ids:
                # Modo piloto: ni siquiera se pide el detalle (get_execution)
                # de ejecuciones de workflows no criticos -- reduce la carga
                # sobre la API de n8n y el volumen de items en Zabbix. Solo
                # se actualiza el checkpoint. Ver CLAUDE.md.
                if not newest_id or exec_id > newest_id:
                    newest_id = exec_id
                continue

            full = get_execution(summary['id'])
            duration_ms = compute_duration_ms(full)
            failed = is_failed(full)
            node_timings = extract_node_timings(full)
            workflow_id = full.get('workflowId')

            items.append({
                'host': zabbix_host,
                'key': 'n8n.workflow.duration.last[{}]'.format(workflow_id),
                'value': duration_ms if duration_ms is not None else 0,
            })
            items.append({
                'host': zabbix_host,
                'key': 'n8n.workflow.status.last[{}]'.format(workflow_id),
                'value': 1 if failed else 0,
            })

            total_count, error_count = register_execution_count(state, workflow_id, exec_id, failed)
            items.append({
                'host': zabbix_host,
                'key': 'n8n.workflow.executions.count[{}]'.format(workflow_id),
                'value': total_count,
            })
            items.append({
                'host': zabbix_host,
                'key': 'n8n.workflow.executions.errors[{}]'.format(workflow_id),
                'value': error_count,
            })

            if node_timings is not None:
                items.append({
                    'host': zabbix_host,
                    'key': 'n8n.workflow.nodes.timing[{}]'.format(workflow_id),
                    'value': json.dumps(node_timings, ensure_ascii=False),
                })

                if str(workflow_id) in critical_workflow_ids:
                    wf_id = str(workflow_id)
                    known = set(state['criticalNodes'].get(wf_id, []))
                    for nt in node_timings:
                        items.append({
                            'host': zabbix_host,
                            'key': 'n8n.node.duration[{},{}]'.format(workflow_id, nt['node']),
                            'value': nt['ms'] if nt['ms'] is not None else 0,
                        })
                        known.add(nt['node'])
                    state['criticalNodes'][wf_id] = sorted(known)

            if failed:
                failed_node = next((n for n in (node_timings or []) if n['status'] == 'error'), None)
                items.append({
                    'host': zabbix_host,
                    'key': 'n8n.workflow.error.last[{}]'.format(workflow_id),
                    'value': json.dumps({
                        'executionId': full.get('id'),
                        'node': failed_node['node'] if failed_node else None,
                        'message': (failed_node or {}).get('errorMessage') or 'error sin detalle de nodo',
                    }, ensure_ascii=False),
                })

            if not newest_id or exec_id > newest_id:
                newest_id = exec_id
            processed_count += 1

        cursor = page.get('nextCursor') or None

        if not last_id:
            # Primera corrida (sin checkpoint todavia): solo se procesa la
            # primera pagina para no traer todo el historico de la instancia.
            break
        if any(int(e['id']) <= last_id for e in batch):
            break
        if not cursor:
            break

    if min_pending_id is not None and (newest_id is None or newest_id >= min_pending_id):
        # Hay una ejecucion sin terminar mas vieja que lo que el checkpoint
        # querria marcar como visto -- lo retenemos justo antes de ella para
        # no perdernos su resultado final cuando termine.
        newest_id = min_pending_id - 1

    # Reenviar en cada corrida el set completo de nodos conocidos por
    # workflow critico (no solo los vistos ahora) para que Zabbix no marque
    # como "perdidos" los items ya descubiertos cuando un workflow no tuvo
    # ejecuciones nuevas en este ciclo. Ver n8n.node.discovery en el template.
    workflow_names = state.get('criticalWorkflowNames', {})
    node_discovery_data = [
        {
            '{#WORKFLOW_ID}': workflow_id,
            '{#NODE_NAME}': node_name,
            '{#WORKFLOW_NAME}': workflow_names.get(workflow_id, workflow_id),
        }
        for workflow_id, nodes in state['criticalNodes'].items()
        if workflow_id in critical_workflow_ids
        for node_name in nodes
    ]
    if node_discovery_data:
        items.append({
            'host': zabbix_host,
            'key': 'n8n.node.discovery',
            'value': json.dumps({'data': node_discovery_data}, ensure_ascii=False),
        })

    if items:
        send_to_zabbix(zabbix_config['host'], zabbix_config['port'], zabbix_config['binPath'], items)

    if (newest_id and newest_id != last_id) or state['criticalNodes']:
        # Actualizar el dict cargado (no armar uno nuevo) para no pisar otras
        # claves ya persistidas, como criticalWorkflowIds (la guarda
        # discovery.py, via monitor.save_critical_ids). Un save_state con un
        # dict "limpio" acá borraba esa clave en cada corrida de
        # `executions`, haciendo que ONLY_CRITICAL viera un set vacio en
        # cuanto discovery y executions corrieran como procesos cron
        # separados (siempre, salvo en --mode all).
        state['lastExecutionId'] = newest_id if newest_id else last_id
        save_state(state)

    return {'processedCount': processed_count}
