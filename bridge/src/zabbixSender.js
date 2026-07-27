'use strict';

const net = require('net');

const HEADER = Buffer.from('ZBXD\x01', 'ascii');

function buildPacket(payload) {
  const jsonPayload = Buffer.from(JSON.stringify(payload), 'utf8');
  const lengthField = Buffer.alloc(8);
  lengthField.writeBigUInt64LE(BigInt(jsonPayload.length), 0);
  return Buffer.concat([HEADER, lengthField, jsonPayload]);
}

function parseResponse(buffer) {
  // Respuesta con el mismo framing: "ZBXD\x01" + 8 bytes de longitud + JSON
  const bodyStart = HEADER.length + 8;
  const body = buffer.slice(bodyStart).toString('utf8');
  return JSON.parse(body);
}

// items: [{ host, key, value, clock? }]
function sendToZabbix({ host, port, items, timeoutMs = 10000 }) {
  if (process.env.DRY_RUN === 'true') {
    console.log('[DRY_RUN] items que se enviarian a Zabbix:');
    console.log(JSON.stringify(items, null, 2));
    return Promise.resolve({ response: 'dry-run', info: `${items.length} items (no enviados)` });
  }

  if (!items.length) {
    return Promise.resolve({ response: 'success', info: 'sin items para enviar' });
  }

  const payload = {
    request: 'sender data',
    data: items,
    clock: Math.floor(Date.now() / 1000),
  };
  const packet = buildPacket(payload);

  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port }, () => {
      socket.write(packet);
    });

    const chunks = [];
    socket.setTimeout(timeoutMs, () => {
      socket.destroy();
      reject(new Error(`Timeout conectando a Zabbix Server ${host}:${port}`));
    });

    socket.on('data', (chunk) => chunks.push(chunk));

    socket.on('end', () => {
      const buffer = Buffer.concat(chunks);
      try {
        resolve(parseResponse(buffer));
      } catch (err) {
        resolve({ response: 'unknown', raw: buffer.toString('utf8') });
      }
    });

    socket.on('error', reject);
  });
}

module.exports = { sendToZabbix, buildPacket, parseResponse };
