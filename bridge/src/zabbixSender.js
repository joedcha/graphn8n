'use strict';

const { execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// En vez de reimplementar el protocolo trapper de Zabbix a mano, este modulo
// delega en el binario `zabbix_sender` (el mismo que ya se usa en el
// servidor externo de metricas, con el patron `-i archivo -T`). Es el mismo
// mecanismo ya probado en produccion, solo que el archivo de entrada lo
// genera el bridge en cada ciclo en vez de armarse manualmente.

function quoteField(value) {
  const str = String(value);
  if (str.length === 0 || /[\s"]/.test(str)) {
    return '"' + str.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }
  return str;
}

// Parsea la linea de resumen que imprime zabbix_sender, por ejemplo:
//   "info from server: "processed: 5; failed: 0; total: 5; seconds spent: 0.000030""
function parseSenderOutput(stdout) {
  const match = stdout.match(/processed:\s*(\d+);\s*failed:\s*(\d+);\s*total:\s*(\d+)/);
  if (match) {
    const failed = Number(match[2]);
    return {
      response: failed === 0 ? 'success' : 'partial',
      processed: Number(match[1]),
      failed,
      total: Number(match[3]),
      raw: stdout.trim(),
    };
  }
  return { response: 'unknown', raw: stdout.trim() };
}

// items: [{ host, key, value, clock? }]
function sendToZabbix({ host, port, binPath, items, timeoutMs = 15000 }) {
  if (process.env.DRY_RUN === 'true') {
    console.log('[DRY_RUN] items que se enviarian a Zabbix:');
    console.log(JSON.stringify(items, null, 2));
    return Promise.resolve({ response: 'dry-run', info: `${items.length} items (no enviados)` });
  }

  if (!items.length) {
    return Promise.resolve({ response: 'success', info: 'sin items para enviar' });
  }

  const defaultClock = Math.floor(Date.now() / 1000);
  const lines = items.map((item) => {
    const clock = item.clock || defaultClock;
    return [quoteField(item.host), quoteField(item.key), clock, quoteField(item.value)].join(' ');
  });

  const tmpFile = path.join(
    os.tmpdir(),
    `n8n-zabbix-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`
  );
  fs.writeFileSync(tmpFile, lines.join('\n') + '\n', 'utf8');

  const bin = binPath || process.env.ZABBIX_SENDER_BIN || 'zabbix_sender';
  const args = ['-z', host, '-i', tmpFile, '-T'];
  if (port) args.push('-p', String(port));

  return new Promise((resolve, reject) => {
    execFile(bin, args, { timeout: timeoutMs }, (err, stdout, stderr) => {
      fs.unlink(tmpFile, () => {});
      if (err) {
        reject(new Error(`zabbix_sender fallo (${bin} ${args.join(' ')}): ${err.message}\n${stderr}`));
        return;
      }
      resolve(parseSenderOutput(stdout));
    });
  });
}

module.exports = { sendToZabbix, quoteField, parseSenderOutput };
