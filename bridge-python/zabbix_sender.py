# -*- coding: utf-8 -*-
"""Envio de items a Zabbix via el binario zabbix_sender.

Puerto 1:1 de bridge/src/zabbixSender.js. No reimplementa el protocolo
trapper: escribe un archivo temporal en formato `-i archivo -T` y shellea al
binario real (mismo mecanismo ya usado en produccion para otros ETLs de
/opt/bmc/ETLs).
"""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid


def _quote_field(value):
    # El formato de archivo de zabbix_sender (-i archivo) es por-linea: un
    # \n o \r literal dentro de un valor rompe el registro y hace fallar el
    # envio completo (todos los items del batch, no solo el problematico).
    # Se reemplaza por un espacio como salvaguarda -- los llamadores no
    # deberian mandar valores multilinea reales a proposito (ver capacity.py,
    # que en cambio usa la secuencia de escape de 2 caracteres "\n" literal,
    # que Zabbix SI convierte a salto de linea real al guardar el item).
    s = str(value).replace('\r\n', ' ').replace('\n', ' ').replace('\r', ' ')
    if s == '' or re.search(r'[\s"]', s):
        # Duplicar cada backslash protege contra que Zabbix interprete un
        # backslash "de verdad" (parte del dato) como el inicio de un
        # escape -- EXCEPTO cuando ya es la secuencia intencional \n o \r
        # (ver arriba), que debe llegar intacta (un solo backslash) para
        # que Zabbix la convierta en salto de linea real.
        s = re.sub(r'\\(?![nr])', r'\\\\', s)
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _parse_sender_output(stdout):
    m = re.search(r'processed:\s*(\d+);\s*failed:\s*(\d+);\s*total:\s*(\d+)', stdout)
    if m:
        failed = int(m.group(2))
        return {
            'response': 'success' if failed == 0 else 'partial',
            'processed': int(m.group(1)),
            'failed': failed,
            'total': int(m.group(3)),
            'raw': stdout.strip(),
        }
    return {'response': 'unknown', 'raw': stdout.strip()}


def send_to_zabbix(host, port, bin_path, items, timeout_s=15):
    if os.environ.get('DRY_RUN') == 'true':
        print('[DRY_RUN] items que se enviarian a Zabbix:')
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return {'response': 'dry-run', 'info': '{} items (no enviados)'.format(len(items))}

    if not items:
        return {'response': 'success', 'info': 'sin items para enviar'}

    default_clock = int(time.time())
    lines = []
    for item in items:
        clock = item.get('clock') or default_clock
        lines.append(' '.join([
            _quote_field(item['host']),
            _quote_field(item['key']),
            str(clock),
            _quote_field(item['value']),
        ]))

    tmp_file = os.path.join(
        tempfile.gettempdir(),
        'n8n-zabbix-{}-{}.txt'.format(int(time.time() * 1000), uuid.uuid4().hex[:8]),
    )
    with open(tmp_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    bin_name = bin_path or os.environ.get('ZABBIX_SENDER_BIN') or 'zabbix_sender'
    args = [bin_name, '-z', host, '-i', tmp_file, '-T']
    if port:
        args += ['-p', str(port)]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
    finally:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass

    if result.returncode != 0:
        raise RuntimeError('zabbix_sender fallo (rc={}) ({}): stdout={!r} stderr={!r}'.format(
            result.returncode, ' '.join(args), result.stdout, result.stderr))

    parsed = _parse_sender_output(result.stdout)
    print('[zabbix_sender] {}'.format(parsed['raw']))
    return parsed
