# n8n-zabbix-bridge

Servicio que conecta la API de n8n (Community Edition) con Zabbix:

- **Discovery** (`src/discovery.js`): detecta workflows nuevos/existentes en n8n y le avisa a Zabbix vía Low-Level Discovery, para que cree automáticamente los items/triggers de monitoreo (ver `zabbix/n8n_template.yaml`). Es la pieza que garantiza que todo workflow creado quede monitoreado sin configuración manual.
- **Executions** (`src/executions.js`): hace polling de ejecuciones terminadas, extrae la duración total y el tiempo por nodo (`resultData.runData`), y los envía a Zabbix.
- **Governance** (`src/governance.js`): audita qué workflows activos no siguen la convención (Error Workflow asignado, tags `team:`/`criticidad:`) y publica el ratio de cumplimiento.

Sin dependencias npm externas: usa `fetch` nativo de Node para hablar con n8n. Para Zabbix, `src/zabbixSender.js` **no** reimplementa el protocolo trapper — genera un archivo temporal con el mismo formato que usa `-i archivo -T` y llama al binario `zabbix_sender` ya instalado en el servidor (el mismo mecanismo que ya usan para cargar otras métricas), en vez de agregar una implementación propia sin probar contra el Zabbix real.

## Estado de este código

Fue escrito sin acceso de red al n8n real ni al Zabbix real (el entorno donde se generó no puede alcanzar hosts internos). La lógica sigue la documentación pública de n8n y el protocolo documentado de Zabbix Sender, pero **hay que validarla contra la instancia real antes de confiar en ella**, en particular:

- La forma exacta del JSON que devuelve `GET /api/v1/executions/{id}?includeData=true` (dónde queda `runData` puede variar según versión de n8n). `executions.js` prueba varias rutas conocidas y loguea un warning con las claves recibidas si no encuentra ninguna — usar ese log para ajustar `extractNodeTimings` si hace falta.
- El framing exacto del protocolo Zabbix Sender puede variar levemente entre versiones de Zabbix muy viejas; probado solo contra la especificación documentada.

## Primer chequeo (sin tocar Zabbix)

1. `cp .env.example .env` y completar `N8N_BASE_URL` y `N8N_API_KEY`. Dejar `DRY_RUN=true`.
2. `npm start` (Node ≥ 18).
3. Debería listar tus workflows y, si hay ejecuciones recientes, loguear (por DRY_RUN) los items que se enviarían a Zabbix — revisar que `n8n.workflow.nodes.timing[...]` tenga datos reales de tus nodos. Si sale el warning de "no se encontro runData", pegar un ejemplo real de la respuesta de `/executions/{id}?includeData=true` para ajustar el parseo.

## Conectar con Zabbix

1. Completar `ZABBIX_SERVER_HOST` (host/IP del Zabbix Server o Proxy, **no** el frontend web), `ZABBIX_SENDER_BIN` (ruta al binario `zabbix_sender` en ese servidor) y `ZABBIX_HOST_NAME` (el nombre exacto del host de Zabbix que va a recibir estos datos).
2. Importar `../zabbix/n8n_template.yaml` en Zabbix (Data collection → Templates → Import). Revisar los macros `{$N8N.DURATION.SLO.MS}`, `{$N8N.HEARTBEAT.WINDOW}`, `{$N8N.GOVERNANCE.MIN_RATIO}`.
3. Crear (o editar) el host de Zabbix con el nombre igual a `ZABBIX_HOST_NAME`, y asociarle la plantilla `Template App n8n Bridge`.
4. Poner `DRY_RUN=false` y correr de nuevo. Verificar en Zabbix (Monitoring → Latest data) que empiecen a aparecer los items descubiertos por workflow.

## Correr con Docker

```bash
docker build -t n8n-zabbix-bridge .
docker run --env-file .env -v n8n-zabbix-bridge-state:/data n8n-zabbix-bridge
```

## Variables de entorno

Ver `.env.example` para la lista completa y valores por defecto.

## Workflows críticos (tiempos por nodo históricos)

Los workflows con el tag `criticidad:alta` (configurable vía `CRITICAL_TAG`) además reciben items individuales `n8n.node.duration[<workflow_id>,<node_name>]` por cada nodo, para tener series históricas por nodo y no solo el detalle de la última ejecución. Esto es deliberadamente selectivo para no disparar la cantidad de items (NVPS) en Zabbix — ver §8 del plan (`docs/plan-observabilidad-n8n-grafana.md`).
