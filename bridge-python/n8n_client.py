# -*- coding: utf-8 -*-
"""Cliente minimo contra la API publica de n8n (/api/v1).

Puerto 1:1 de bridge/src/n8nClient.js. Solo libreria estandar (urllib), sin
dependencias externas -- mismo criterio que el bridge Node (ver CLAUDE.md,
"No external npm dependencies by design").
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request


def _config():
    base_url = os.environ.get('N8N_BASE_URL')
    api_key = os.environ.get('N8N_API_KEY')
    if not base_url or not api_key:
        raise RuntimeError('Faltan N8N_BASE_URL y/o N8N_API_KEY en el entorno')
    return base_url.rstrip('/'), api_key


def _ssl_context():
    # N8N_VERIFY_TLS=false deshabilita la verificacion del certificado.
    # Usar SOLO si se confirmo que el cert de la instancia esta vencido y
    # todavia no se pudo renovar -- ver CLAUDE.md.
    if os.environ.get('N8N_VERIFY_TLS', 'true').strip().lower() == 'false':
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def n8n_get(path, params=None):
    base_url, api_key = _config()
    query = ''
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            query = '?' + urllib.parse.urlencode(clean)
    url = '{}/api/v1{}{}'.format(base_url, path, query)
    req = urllib.request.Request(
        url, headers={'X-N8N-API-KEY': api_key, 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:200]
        raise RuntimeError('n8n API {} -> HTTP {}: {}'.format(path, e.code, body)) from e


def list_workflows():
    results = []
    cursor = None
    while True:
        page = n8n_get('/workflows', {'limit': 100, 'cursor': cursor})
        results.extend(page.get('data') or [])
        cursor = page.get('nextCursor')
        if not cursor:
            break
    return results


def list_executions(cursor=None):
    """Ejecuciones terminadas (exito o error), sin el detalle por nodo."""
    return n8n_get('/executions', {'limit': 50, 'cursor': cursor, 'includeData': 'false'})


def get_execution(execution_id):
    """Detalle completo, incluyendo resultData.runData con tiempos por nodo."""
    return n8n_get('/executions/{}'.format(execution_id), {'includeData': 'true'})
