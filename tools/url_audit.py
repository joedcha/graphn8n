# -*- coding: utf-8 -*-
"""Inventario de URLs/destinos de red configurados en n8n.

Objetivo: sacar TODAS las URLs que los workflows pueden llegar a invocar, para
armar una whitelist de salida (solo se permitira lo que este aqui).

Que recorre:
  1. Todos los workflows (`/api/v1/workflows`), nodo por nodo, recorriendo
     recursivamente TODO el arbol de `parameters` -- no solo el campo `url`.
     Cubre httpRequest, webhook, graphql, code (fetch/axios dentro del JS),
     executeCommand (curl/wget), resource locators, headers, bodies, etc.
  2. La version publicada (`activeVersion.nodes`) ademas del borrador (`nodes`),
     porque en un workflow activo lo que corre es la publicada y pueden diferir.
  3. Las credenciales (`/api/v1/credentials`): la API publica NO devuelve el
     campo `data`, asi que aqui solo se resuelve el destino por TIPO de
     credencial (tabla CRED_TYPE_HOSTS) y se marca cuales tienen host
     configurable que hay que extraer aparte (ver --cred-data).

Uso:
    python url_audit.py                    # baja de la API y genera los CSV
    python url_audit.py --dump ./dump      # ademas guarda el JSON crudo
    python url_audit.py --from-dump ./dump # reprocesa sin volver a llamar a n8n
    python url_audit.py --cred-data creds_data.json   # suma hosts de credenciales

Lee N8N_BASE_URL / N8N_API_KEY / N8N_VERIFY_TLS del entorno o de un .env
(por defecto ../bridge-python/.env).
"""

import argparse
import csv
import ipaddress
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

# --- Que se considera "no sale a internet" -----------------------------------
# Ajustar a la realidad de la red antes de usar el resultado como whitelist.
SUFIJOS_INTERNOS = (
    '.nh.inet', '.inet', '.local', '.localdomain', '.svc', '.cluster.local',
    '.nip.io', '.intranet',
)
# Dominios propios de Telefonica: resuelven por DNS publico (o sea, salen a
# internet igual que un tercero), pero se separan para que seguridad los pueda
# aprobar en bloque en vez de uno por uno junto a sendgrid/openai/etc.
SUFIJOS_CORPORATIVOS = (
    'telefonicawebsites.co', 'movistar.com.co', 'telefonica.com', 'telefonica.co',
    'telefonica.es', 'movistar.co', 'movistar.com', 'tef.com',
)
HOSTS_INTERNOS_EXACTOS = {'localhost', 'host.docker.internal', 'n8n', '127.0.0.1', '::1'}

# --- Deteccion ----------------------------------------------------------------
ESQUEMAS = (
    'https?', 'wss?', 'ftps?', 'sftp', 'ldaps?', 'smtps?', 'imaps?', 'pop3s?',
    'mongodb(?:\\+srv)?', 'postgres(?:ql)?', 'redis(?:s)?', 'mysql', 'amqps?',
    'mqtts?', 'grpc', 's3', 'kafka',
)
RE_URL = re.compile(r'(?:' + '|'.join(ESQUEMAS) + r')://[^\s"\'`<>\\(){}\[\],;]+', re.I)

# Claves cuyo valor es un destino de red aunque no traiga esquema (ej. "host": "10.1.2.3")
RE_CLAVE_HOST = re.compile(
    r'^(url|uri|endpoint|endpointurl|baseurl|base_url|apiurl|api_url|host|hostname|'
    r'server|servidor|domain|dominio|webhookurl|webhook_url|instanceurl|serverurl|'
    r'address|direccion|resourcename|accountname|bucketendpoint|brokers|'
    r'bootstrapservers|connectionstring|dsn)$', re.I)
# Valor plausible de host/IP (con punto, o localhost), opcional :puerto y /path
RE_HOST_SUELTO = re.compile(
    r'^(?:[a-z0-9_](?:[a-z0-9\-_]*[a-z0-9])?\.)+[a-z]{2,}(?::\d{1,5})?(?:/[^\s]*)?$', re.I)
RE_IP_PUERTO = re.compile(r'^(\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?(?:/[^\s]*)?$')

# Claves a ignorar: no son destinos aunque matcheen (imagenes de UI, iconos, etc.)
RE_CLAVE_RUIDO = re.compile(r'(icon|image|avatar|logo|schema|\$schema|mimetype)$', re.I)

# Una URL precedida por esto es un identificador XML/JSON-Schema, NO una llamada
# de red: nadie la resuelve en runtime. Se marca aparte para no meterla en la
# whitelist por error (ej. http://www.w3.org/..., http://schemas.xmlsoap.org/...).
RE_NAMESPACE = re.compile(
    r'(xmlns[:a-z0-9_\-]*\s*=|targetNamespace|schemaLocation|\$schema|xsi:|'
    r'SYSTEM)\s*[\\"\':]*\s*$', re.I)
# En un DOCTYPE la URL va despues del identificador publico, no pegada a la
# palabra clave, asi que este no puede ir anclado al final de la ventana.
RE_NAMESPACE_LIBRE = re.compile(r'<!DOCTYPE|//DTD |\$schema', re.I)

# Una clave "host"/"server"/"domain" dentro de estas rutas es un campo de DATOS
# (columna de una tabla, input de un tool, campo de un Set), no un destino de red.
RE_RUTA_DATOS = re.compile(
    r'\.(columns|workflowInputs|values|fields|assignments|schema|matchingColumns|'
    r'queryParameters|headerParameters|bodyParameters|conditions|filters)\.', re.I)

# Nodos cuyo texto es documentacion, no trafico real
TIPOS_DOC = {'n8n-nodes-base.stickyNote'}

# --- Destino por tipo de credencial ------------------------------------------
# host fijo -> el tipo determina el destino aunque no veamos el `data`.
# None -> el host es configurable dentro de la credencial: hay que extraerlo
#         aparte (UI / API interna / n8n export:credentials --decrypted).
CRED_TYPE_HOSTS = {
    'telegramApi': 'api.telegram.org',
    'openAiApi': 'api.openai.com',
    'deepSeekApi': 'api.deepseek.com',
    'groqApi': 'api.groq.com',
    'anthropicApi': 'api.anthropic.com',
    'googlePalmApi': 'generativelanguage.googleapis.com',
    'slackApi': 'slack.com',
    'slackOAuth2Api': 'slack.com',
    'githubApi': 'api.github.com',
    'microsoftOutlookOAuth2Api': 'graph.microsoft.com',
    'microsoftGraphSecurityOAuth2Api': 'graph.microsoft.com',
    'microsoftTeamsOAuth2Api': 'graph.microsoft.com',
    'googleSheetsOAuth2Api': 'sheets.googleapis.com',
    'googleDriveOAuth2Api': 'www.googleapis.com',
    'googleApi': 'www.googleapis.com',
    'jiraSoftwareCloudApi': None,
    'azureOpenAiApi': None,        # <resourceName>.openai.azure.com
    'azureStorageSharedKeyApi': None,
    'ollamaApi': None,             # baseUrl
    'zabbixApi': None,             # url
    'n8nApi': None,                # baseUrl
    'postgres': None, 'mySql': None, 'microsoftSql': None, 'oracleDBApi': None,
    'redis': None, 'kafka': None, 'mongoDb': None, 'elasticsearchApi': None,
    'smtp': None, 'imap': None, 'ftp': None, 'sftp': None, 'ssh': None,
    'sshPassword': None, 'sshPrivateKey': None, 'ldap': None, 'mqtt': None,
    'qdrantApi': None, 'supabaseApi': None, 'grafanaApi': None,
    # sin host propio: el destino lo pone el nodo que las usa
    'httpBasicAuth': '(sin host: lo define el nodo)',
    'httpHeaderAuth': '(sin host: lo define el nodo)',
    'httpBearerAuth': '(sin host: lo define el nodo)',
    'httpDigestAuth': '(sin host: lo define el nodo)',
    'httpQueryAuth': '(sin host: lo define el nodo)',
    'httpCustomAuth': '(sin host: lo define el nodo)',
    'oAuth2Api': None,             # authUrl / accessTokenUrl
    'oAuth1Api': None,
}


# ==============================================================================
# API n8n
# ==============================================================================
def cargar_env(ruta):
    if not ruta or not os.path.exists(ruta):
        return
    with open(ruta, encoding='utf-8') as fh:
        for linea in fh:
            linea = linea.strip()
            if linea and not linea.startswith('#') and '=' in linea:
                k, v = linea.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def _ctx():
    if os.environ.get('N8N_VERIFY_TLS', 'true').strip().lower() == 'false':
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return ssl.create_default_context()


def api_get(path):
    base = os.environ['N8N_BASE_URL'].rstrip('/')
    req = urllib.request.Request(
        base + path,
        headers={'X-N8N-API-KEY': os.environ['N8N_API_KEY'], 'Accept': 'application/json'})
    with urllib.request.urlopen(req, context=_ctx(), timeout=120) as res:
        return json.loads(res.read().decode('utf-8'))


def traer_todo(path, limite=100):
    datos, cursor = [], None
    while True:
        sep = '&' if '?' in path else '?'
        q = '{}{}limit={}'.format(path, sep, limite) + ('&cursor=' + cursor if cursor else '')
        pag = api_get(q)
        datos.extend(pag.get('data') or [])
        cursor = pag.get('nextCursor')
        if not cursor:
            break
    return datos


# ==============================================================================
# Extraccion
# ==============================================================================
def recorrer(obj, prefijo=''):
    """Genera (ruta, clave, valor_string) por cada string del arbol."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from recorrer(v, '{}.{}'.format(prefijo, k) if prefijo else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from recorrer(v, '{}[{}]'.format(prefijo, i))
    elif isinstance(obj, str):
        clave = prefijo.rsplit('.', 1)[-1].split('[')[0]
        yield prefijo, clave, obj


def es_expresion(valor):
    return valor.startswith('=') or '{{' in valor


def partir_host(url):
    """Devuelve (esquema, host) tolerando expresiones n8n dentro de la URL."""
    m = re.match(r'^([a-z0-9+]+)://(.*)$', url, re.I)
    esquema, resto = (m.group(1).lower(), m.group(2)) if m else ('', url)
    host = re.split(r'[/?#]', resto, 1)[0]
    host = host.split('@')[-1]          # credenciales embebidas user:pass@host
    host = host.rstrip('.').strip()
    if '{{' in host or host.startswith('$'):
        return esquema, 'DINAMICO'
    return esquema, host.lower()


def _coincide(host, dominios):
    """True si host es el dominio o un subdominio suyo (acepta '.x.com' o 'x.com')."""
    for d in dominios:
        d = d.lstrip('.')
        if host == d or host.endswith('.' + d):
            return True
    return False


def clasificar(host):
    if not host or host == 'DINAMICO':
        return 'DINAMICO'
    solo_host = host.split(':')[0]
    if solo_host in HOSTS_INTERNOS_EXACTOS:
        return 'INTERNA'
    try:
        ip = ipaddress.ip_address(solo_host)
        return 'INTERNA' if (ip.is_private or ip.is_loopback or ip.is_link_local) else 'EXTERNA'
    except ValueError:
        pass
    if _coincide(solo_host, SUFIJOS_INTERNOS):
        return 'INTERNA'
    if _coincide(solo_host, SUFIJOS_CORPORATIVOS):
        return 'CORPORATIVA'
    if '.' not in solo_host:
        return 'INTERNA'          # hostname corto = resolucion interna
    return 'EXTERNA'


def hallazgos_de_valor(ruta, clave, valor):
    """Devuelve lista de (esquema, host, valor_recortado, dinamica, namespace)."""
    out = []
    if RE_CLAVE_RUIDO.search(clave):
        return out
    encontradas = list(RE_URL.finditer(valor))
    for m in encontradas:
        u = m.group(0).rstrip('.,;\'")')
        esquema, host = partir_host(u)
        # ventana amplia: en un DOCTYPE el identificador PUBLIC va bastante
        # antes de la URL ("<!DOCTYPE html PUBLIC "-//W3C//DTD ...EN" "http://...")
        prev = valor[max(0, m.start() - 120):m.start()]
        ns = bool(RE_NAMESPACE.search(prev) or RE_NAMESPACE_LIBRE.search(prev))
        out.append((esquema, host, u[:500], es_expresion(valor) or '{{' in u, ns))
    if not encontradas and RE_CLAVE_HOST.match(clave):
        v = valor.strip()
        if not v or len(v) > 300 or RE_RUTA_DATOS.search(ruta):
            return out
        if es_expresion(v):
            out.append(('', 'DINAMICO', v[:500], True, False))
        elif RE_HOST_SUELTO.match(v) or RE_IP_PUERTO.match(v):
            _, host = partir_host(v)
            out.append(('', host, v[:500], False, False))
    return out


def escanear_nodos(nodos, wf, version, filas, uso_cred):
    for nodo in nodos or []:
        ntipo = nodo.get('type', '')
        origen = 'documentacion' if ntipo in TIPOS_DOC else 'nodo'
        for cred_tipo, cred in (nodo.get('credentials') or {}).items():
            uso_cred[(cred_tipo, (cred or {}).get('name', ''))] += 1
        ambito = {'parameters': nodo.get('parameters') or {}}
        for ruta, clave, valor in recorrer(ambito):
            for esquema, host, texto, dinamica, ns in hallazgos_de_valor(ruta, clave, valor):
                filas.append({
                    'workflow_id': wf.get('id'),
                    'workflow': wf.get('name'),
                    'activo': 'si' if wf.get('active') else 'no',
                    'archivado': 'si' if wf.get('isArchived') else 'no',
                    'version': version,
                    'nodo': nodo.get('name'),
                    'tipo_nodo': ntipo,
                    'nodo_deshabilitado': 'si' if nodo.get('disabled') else 'no',
                    'parametro': ruta,
                    'esquema': esquema,
                    'host': host,
                    'categoria': 'NAMESPACE_XML' if ns else clasificar(host),
                    'dinamica': 'si' if dinamica else 'no',
                    'origen': 'namespace' if ns else origen,
                    'valor': texto,
                })


def escanear_credenciales(creds, cred_data, filas):
    """cred_data: {id_or_name: {campo: valor}} extraido aparte (opcional)."""
    for c in creds:
        tipo = c.get('type', '')
        nombre = c.get('name', '')
        datos = (cred_data or {}).get(c.get('id')) or (cred_data or {}).get(nombre)
        encontrado = False
        if datos:
            for ruta, clave, valor in recorrer(datos):
                for esquema, host, texto, dinamica, _ns in hallazgos_de_valor(ruta, clave, valor):
                    encontrado = True
                    filas.append({
                        'workflow_id': '', 'workflow': '', 'activo': '', 'archivado': '',
                        'version': '', 'nodo': nombre, 'tipo_nodo': 'credencial:' + tipo,
                        'nodo_deshabilitado': 'no', 'parametro': ruta,
                        'esquema': esquema, 'host': host, 'categoria': clasificar(host),
                        'dinamica': 'si' if dinamica else 'no',
                        'origen': 'credencial', 'valor': texto,
                    })
        if encontrado:
            continue
        fijo = CRED_TYPE_HOSTS.get(tipo, None)
        if fijo and not fijo.startswith('('):
            host, cat, val = fijo, clasificar(fijo), 'destino fijo del tipo ' + tipo
        elif fijo:
            continue                      # tipo sin host propio (httpBasicAuth, etc.)
        else:
            host, cat, val = 'PENDIENTE', 'PENDIENTE', 'host configurable dentro de la credencial'
        filas.append({
            'workflow_id': '', 'workflow': '', 'activo': '', 'archivado': '', 'version': '',
            'nodo': nombre, 'tipo_nodo': 'credencial:' + tipo, 'nodo_deshabilitado': 'no',
            'parametro': '(tipo de credencial)', 'esquema': '', 'host': host,
            'categoria': cat, 'dinamica': 'no', 'origen': 'credencial', 'valor': val,
        })


# ==============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', default=os.path.join(os.path.dirname(__file__), '..', 'bridge-python', '.env'))
    ap.add_argument('--dump', help='carpeta donde guardar el JSON crudo bajado de n8n')
    ap.add_argument('--from-dump', help='carpeta con workflows.json/credentials.json ya bajados')
    ap.add_argument('--cred-data', help='JSON {credId|nombre: {campo: valor}} con datos de credenciales')
    ap.add_argument('--out', default='.', help='carpeta de salida de los CSV')
    args = ap.parse_args()

    if args.from_dump:
        wfs = json.load(open(os.path.join(args.from_dump, 'workflows.json'), encoding='utf-8'))
        creds = json.load(open(os.path.join(args.from_dump, 'credentials.json'), encoding='utf-8'))
    else:
        cargar_env(args.env)
        if not os.environ.get('N8N_BASE_URL') or not os.environ.get('N8N_API_KEY'):
            sys.exit('Faltan N8N_BASE_URL / N8N_API_KEY')
        wfs = traer_todo('/api/v1/workflows')
        creds = traer_todo('/api/v1/credentials', limite=250)
        if args.dump:
            os.makedirs(args.dump, exist_ok=True)
            json.dump(wfs, open(os.path.join(args.dump, 'workflows.json'), 'w', encoding='utf-8'), ensure_ascii=False)
            json.dump(creds, open(os.path.join(args.dump, 'credentials.json'), 'w', encoding='utf-8'), ensure_ascii=False)

    cred_data = json.load(open(args.cred_data, encoding='utf-8')) if args.cred_data else None

    filas, uso_cred = [], Counter()
    for wf in wfs:
        escanear_nodos(wf.get('nodes'), wf, 'borrador', filas, uso_cred)
        av = wf.get('activeVersion') or {}
        if av.get('nodes'):
            escanear_nodos(av['nodes'], wf, 'publicada', filas, uso_cred)
    escanear_credenciales(creds, cred_data, filas)

    # deduplicar: mismo workflow+nodo+host+valor en borrador y publicada
    vistos, unicas = set(), []
    for f in filas:
        k = (f['workflow_id'], f['nodo'], f['parametro'], f['valor'])
        if k in vistos:
            continue
        vistos.add(k)
        unicas.append(f)
    filas = unicas

    os.makedirs(args.out, exist_ok=True)
    campos = ['categoria', 'host', 'esquema', 'dinamica', 'origen', 'workflow', 'workflow_id',
              'activo', 'archivado', 'version', 'nodo', 'tipo_nodo', 'nodo_deshabilitado',
              'parametro', 'valor']
    det = os.path.join(args.out, 'n8n_urls_detalle.csv')
    with open(det, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(sorted(filas, key=lambda r: (r['categoria'], r['host'], r['workflow'] or '')))

    por_host = defaultdict(lambda: {'n': 0, 'wfs': set(), 'origenes': set(), 'ejemplo': ''})
    for f in filas:
        h = por_host[(f['categoria'], f['host'])]
        h['n'] += 1
        if f['workflow']:
            h['wfs'].add(f['workflow'])
        h['origenes'].add(f['origen'])
        if not h['ejemplo'] and f['esquema']:
            h['ejemplo'] = f['valor']
    uni = os.path.join(args.out, 'n8n_urls_por_host.csv')
    with open(uni, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['categoria', 'host', 'apariciones', 'workflows', 'origenes', 'ejemplo', 'lista_workflows'])
        for (cat, host), d in sorted(por_host.items(), key=lambda x: (x[0][0], -x[1]['n'])):
            w.writerow([cat, host, d['n'], len(d['wfs']), '|'.join(sorted(d['origenes'])),
                        d['ejemplo'], ' | '.join(sorted(d['wfs'])[:40])])

    print('workflows: {}   credenciales: {}   hallazgos: {}'.format(len(wfs), len(creds), len(filas)))
    print('hosts unicos: {}'.format(len(por_host)))
    for cat, n in Counter(f['categoria'] for f in filas).most_common():
        hosts = len({h for c, h in por_host if c == cat})
        print('  {:<12} {:>5} hallazgos  {:>4} hosts'.format(cat, n, hosts))
    # Destinos que NO se pueden acotar estaticamente: el host sale de una
    # expresion en runtime. Son los que hay que revisar a mano para la whitelist.
    rev = [f for f in filas if f['host'] == 'DINAMICO']
    rutaie = os.path.join(args.out, 'n8n_urls_dinamicas_revisar.csv')
    with open(rutaie, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction='ignore')
        w.writeheader()
        w.writerows(sorted(rev, key=lambda r: (r['activo'] != 'si', r['workflow'] or '')))

    pend = Counter(f['tipo_nodo'] for f in filas if f['categoria'] == 'PENDIENTE')
    if pend:
        print('\nCredenciales con host configurable que NO expone la API publica')
        print('(hay que extraerlas de la UI / API interna / export:credentials):')
        for t, n in pend.most_common():
            print('  {:<45} {}'.format(t.replace('credencial:', ''), n))
    din = [f for f in filas if f['dinamica'] == 'si']
    if din:
        print('\nURLs construidas por expresion (no acotables estaticamente): {} en {} workflows'
              .format(len(din), len({f['workflow'] for f in din if f['workflow']})))
    fromai = [f for f in rev if '$fromAI' in f['valor']]
    if fromai:
        print('  !! {} de ellas las decide un modelo en runtime ($fromAI) en {} workflows'
              .format(len(fromai), len({f['workflow'] for f in fromai})))
    print('\nCSV: {}\n     {}\n     {}'.format(det, uni, rutaie))


if __name__ == '__main__':
    main()
