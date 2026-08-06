# -*- coding: utf-8 -*-
"""Analisis pasivo de capacidad: pagina el historial real de /executions de
n8n (solo resumen, sin detalle por nodo) y agrega volumen/errores/duracion
por hora, para tener una primera foto de cuanta carga viene manejando la
instancia sin generar trafico artificial.

Uso: ./python/bin/python3 capacity_snapshot.py [--days N] [--max-pages N]
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import monitor
from n8n_client import n8n_get


def parse_dt(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--max-pages', type=int, default=400)
    args = parser.parse_args()

    monitor.load_dotenv()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    by_hour = defaultdict(lambda: {'total': 0, 'errors': 0, 'duration_sum_ms': 0, 'duration_n': 0})
    by_workflow = defaultdict(int)
    oldest_seen = None
    newest_seen = None
    cursor = None
    total = 0

    for page_num in range(1, args.max_pages + 1):
        page = n8n_get('/executions', {'limit': 100, 'cursor': cursor})
        batch = page.get('data') or []
        if not batch:
            break

        stop = False
        for e in batch:
            started = e.get('startedAt')
            if not started:
                continue
            dt = parse_dt(started)
            if newest_seen is None or dt > newest_seen:
                newest_seen = dt
            if oldest_seen is None or dt < oldest_seen:
                oldest_seen = dt
            if dt < cutoff:
                stop = True
                continue

            hour_key = dt.strftime('%Y-%m-%d %H:00')
            bucket = by_hour[hour_key]
            bucket['total'] += 1
            status = e.get('status')
            if status in ('error', 'crashed'):
                bucket['errors'] += 1
            stopped = e.get('stoppedAt')
            if stopped:
                dur_ms = (parse_dt(stopped) - dt).total_seconds() * 1000
                if dur_ms >= 0:
                    bucket['duration_sum_ms'] += dur_ms
                    bucket['duration_n'] += 1
            by_workflow[e.get('workflowId')] += 1
            total += 1

        print('[pagina {}] acumulado={} mas vieja vista={}'.format(
            page_num, total, oldest_seen.isoformat() if oldest_seen else None), file=sys.stderr)

        cursor = page.get('nextCursor')
        if stop or not cursor:
            break

    hours_sorted = sorted(by_hour.keys())
    result = {
        'window_requested_days': args.days,
        'oldest_execution_seen': oldest_seen.isoformat() if oldest_seen else None,
        'newest_execution_seen': newest_seen.isoformat() if newest_seen else None,
        'total_executions_in_window': total,
        'hours_covered': len(hours_sorted),
        'by_hour': [
            {
                'hour': h,
                'total': by_hour[h]['total'],
                'errors': by_hour[h]['errors'],
                'avg_duration_ms': (by_hour[h]['duration_sum_ms'] / by_hour[h]['duration_n']) if by_hour[h]['duration_n'] else None,
            }
            for h in hours_sorted
        ],
        'top_workflows_by_volume': sorted(by_workflow.items(), key=lambda kv: -kv[1])[:20],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
