# -*- coding: utf-8 -*-
"""Prueba puntual (no forma parte de la suite normal) del fix aplicado a
compute_duration_ms para el caso de ejecuciones reintentadas (retry).

Reproduce con datos sinteticos el patron real observado el 2026-08-19 en
"Call Agent Zabbix": la ejecucion original #2132785 fallo ~14:29 hora local,
se reintento (#2133100) a las ~15:43:18 y el retry reutilizo el runData de
nodos que ya habian corrido bien en el intento original (startTime viejo),
mientras que el resto de nodos tienen startTime real del reintento.

No requiere N8N_API_KEY -- no toca el n8n real, solo prueba la logica ya
aplicada en compute_duration_ms.
"""

from datetime import datetime, timezone, timedelta

from executions import compute_duration_ms

TZ = timezone(timedelta(hours=-5))  # UTC-5, igual que el dashboard de Grafana


def epoch_ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Caso 1 (el incidente real): ejecucion CON retryOf -- el runData mezcla
# timestamps del intento original (14:29) con el reintento real (15:43:18).
# Antes del fix esto daba 74.2 min; debe dar la duracion real del reintento.
# ---------------------------------------------------------------------------

original_start = epoch_ms(2026, 8, 19, 14, 29, 7)     # arranque del intento original que fallo
retry_started_at = epoch_ms(2026, 8, 19, 15, 43, 18)  # arranque real del retry (== execution.startedAt)
retry_stopped_at = retry_started_at + 827             # "Succeeded in 827ms" (visto en n8n)

retry_execution = {
    'id': 2133100,
    'workflowId': 'LOessDlgGW8KQrXY',
    'retryOf': '2132785',
    'status': 'success',
    'finished': True,
    'startedAt': datetime.fromtimestamp(retry_started_at / 1000, tz=TZ).isoformat(),
    'stoppedAt': datetime.fromtimestamp(retry_stopped_at / 1000, tz=TZ).isoformat(),
    'data': {
        'resultData': {
            'runData': {
                # Nodos tempranos: NO se re-ejecutaron en el retry -- n8n
                # reutiliza el runData del intento original con su startTime
                # viejo (14:29).
                'Webhook': [{'startTime': original_start, 'executionTime': 45}],
                'Edit Fields': [{'startTime': original_start + 45, 'executionTime': 12}],
                # Nodo que fallo en el intento original: SI se re-ejecuta,
                # con startTime real del retry (15:43:18).
                'Call Agente MetrIA': [{'startTime': retry_started_at, 'executionTime': 700}],
                'Enviar Correo': [{'startTime': retry_started_at + 700, 'executionTime': 80}],
            }
        }
    },
}

real_ms = retry_stopped_at - retry_started_at
result_ms = compute_duration_ms(retry_execution)

print('--- Caso 1: ejecucion reintentada (retryOf seteado) ---')
print('Duracion real del reintento (n8n UI): {} ms ({:.3f} s)'.format(real_ms, real_ms / 1000))
print('compute_duration_ms(execution):       {} ms'.format(result_ms))
assert result_ms == real_ms, 'deberia dar exactamente la duracion real del retry, no la mezcla con el intento original'
print('OK -- antes del fix esto daba {} ms (~{:.1f} min)'.format(
    int(max(retry_started_at + 700 + 80, original_start + 57) - original_start),
    (max(retry_started_at + 700 + 80, original_start + 57) - original_start) / 60000,
))

# ---------------------------------------------------------------------------
# Caso 2 (control): ejecucion normal (SIN retryOf) con un nodo Wait real,
# donde runData legitimamente tiene un startTime mas temprano que lo que
# startedAt sugeriria (n8n pisa startedAt para reflejar solo el ultimo
# tramo). El fix NO debe alterar este comportamiento -- es el caso para el
# que compute_duration_ms fue escrito originalmente.
# ---------------------------------------------------------------------------

wait_start = epoch_ms(2026, 8, 19, 9, 0, 0)
resume_start = epoch_ms(2026, 8, 19, 9, 6, 0)  # n8n "reinicia" startedAt aqui

wait_execution = {
    'id': 9999999,
    'workflowId': 'LOessDlgGW8KQrXY',
    'status': 'success',
    'finished': True,
    'startedAt': datetime.fromtimestamp(resume_start / 1000, tz=TZ).isoformat(),
    'stoppedAt': datetime.fromtimestamp(resume_start / 1000 + 2, tz=TZ).isoformat(),
    'data': {
        'resultData': {
            'runData': {
                'Trigger': [{'startTime': wait_start, 'executionTime': 20}],
                'Wait': [{'startTime': wait_start + 20, 'executionTime': (resume_start - wait_start - 20)}],
                'Enviar Correo': [{'startTime': resume_start, 'executionTime': 2000}],
            }
        }
    },
}

wait_real_span_ms = (resume_start + 2000) - wait_start  # ~6 min 2s, el span verdadero incluyendo la espera
wait_result_ms = compute_duration_ms(wait_execution)

print()
print('--- Caso 2 (control): ejecucion normal con nodo Wait (sin retryOf) ---')
print('Span real esperado (incluye espera): {} ms ({:.1f} min)'.format(wait_real_span_ms, wait_real_span_ms / 60000))
print('compute_duration_ms(execution):      {} ms'.format(wait_result_ms))
assert wait_result_ms == wait_real_span_ms, 'el fix NO debe cambiar el resultado para ejecuciones sin retryOf'
print('OK -- comportamiento Wait intacto (idéntico a antes del fix).')

print()
print('Todas las verificaciones pasaron.')
