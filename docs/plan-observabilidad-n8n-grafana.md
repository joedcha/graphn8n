# Plan de Observabilidad n8n → Zabbix → Grafana

## 1. Objetivo

Garantizar que **todo** workflow creado en n8n (Community Edition) quede automáticamente monitoreado, con visibilidad de:

- Estado de cada ejecución (éxito / error / esperando / cancelada).
- Tiempo de respuesta total de la ejecución.
- Tiempo de respuesta **por nodo** dentro de cada ejecución.
- Errores y causa raíz.
- Salud de la plataforma n8n (colas, workers, uso de recursos).

**Stack objetivo de la compañía**: Zabbix es la herramienta de métricas/monitoreo (recolección, umbrales, alertas), y Grafana es la herramienta oficial de visualización (dashboards) sobre esos datos, vía el datasource de Zabbix para Grafana. n8n no tiene integración nativa con Zabbix, así que hay que construir el puente.

> **n8n Community Edition**: no tiene Log Streaming / Event Bus (eso es Enterprise), así que la fuente de verdad para tiempos por nodo es la **API REST de ejecuciones** (`/rest/executions`), no el Event Bus. El endpoint `/metrics` (formato Prometheus) sí está disponible en Community y es aprovechable por Zabbix.

---

## 2. Fuentes de datos disponibles en n8n (Community)

| Fuente | Qué entrega | Nivel de detalle |
|---|---|---|
| **Endpoint `/metrics`** (formato Prometheus, se activa por env var) | Contadores/histogramas agregados: ejecuciones por estado, duración, cache, endpoints de la API | Agregado, no por ejecución individual |
| **REST API `/executions` y `/executions/{id}?includeData=true`** | Detalle completo de cada ejecución, incluyendo `resultData.runData` con `startTime` y `executionTime` (ms) **por nodo** | Por ejecución y por nodo — **esta es la fuente clave para "tiempo por nodo"** |
| **REST API `/workflows`** | Listado de workflows, tags, si tienen Error Workflow asignado, si están activos | Metadata — clave para gobernanza |
| **Error Workflow** (Settings → Error Workflow, por workflow) | Se dispara un workflow cuando otro falla, con contexto del error | Por ejecución fallida |
| **Logs de n8n (stdout/archivo)** | Logs de proceso, estructurables como JSON | Nivel proceso |

Zabbix **puede leer el endpoint `/metrics` directamente** (soporta parseo de formato Prometheus desde Zabbix 6.0 vía preprocesamiento "Prometheus pattern" en items HTTP agent), pero **no puede** obtener el detalle por nodo de ahí — para eso se necesita un componente propio que hable con la API REST de ejecuciones y empuje los datos a Zabbix.

---

## 3. Arquitectura propuesta

```
┌──────────────┐   HTTP agent item        ┌─────────┐
│     n8n       │  (scrape /metrics,       │ Zabbix  │
│  (instancia)  │   preprocesamiento       │ Server  │
│               │   "Prometheus pattern") ▶│         │
│               │                          │         │
│   REST API    │                          │         │
│  /workflows   │◀── polling ──┐           │         │
│  /executions  │              │           │         │
└───────┬───────┘              ▼           │         │
        │              ┌───────────────┐   │         │
        │              │  n8n-zabbix-   │   │         │
        │              │   bridge       │──▶│ Zabbix  │
        │              │ (servicio      │   │ Sender/ │
        │              │  propio)       │   │ Trapper │
        │              └───────┬────────┘   │ items   │
        │                      │            │         │
        │                      │ LLD JSON   │         │
        │                      │ (discovery │         │
        │                      │ de workflows)         │
        │                      └───────────▶│         │
        │                                   └────┬────┘
        │  Error Workflow ──▶ trapper item        │
        │  (alerta directa de fallo)               │
        │                                          ▼
        │                                   ┌──────────────┐
        └──────────────────────────────────▶│   Grafana     │
                                             │ (datasource   │
                                             │  Zabbix) →    │
                                             │ Dashboards    │
                                             └──────────────┘
```

**Zabbix hace la recolección, umbrales y alertas** (su rol nativo). **Grafana solo visualiza**, usando el plugin oficial "Zabbix" como datasource — así se respeta que Grafana es la herramienta oficial de visualización sin duplicar el trabajo de alerting que ya sabe hacer Zabbix.

---

## 4. Componente central: `n8n-zabbix-bridge`

Es la única pieza de infraestructura nueva a construir (servicio pequeño en Node.js/Python, puede correr como contenedor junto a n8n). Responsabilidades:

### 4.1 Descubrimiento de workflows (Zabbix Low-Level Discovery)

Cada ciertos minutos, llama a `GET /rest/workflows` y genera el JSON de **LLD** que Zabbix espera:

```json
{"data":[
  {"{#WORKFLOW_ID}":"123","{#WORKFLOW_NAME}":"Facturación-Diaria","{#TEAM}":"finanzas"},
  {"{#WORKFLOW_ID}":"124","{#WORKFLOW_NAME}":"Sync-CRM","{#TEAM}":"ventas"}
]}
```

Lo envía a un **item trapper de discovery** en Zabbix. En Zabbix se define una **regla de descubrimiento** con **prototipos de item/trigger** asociados a `{#WORKFLOW_ID}`:

- Item prototipo: `n8n.workflow.executions.count[{#WORKFLOW_ID}]`
- Item prototipo: `n8n.workflow.duration.last[{#WORKFLOW_ID}]`
- Item prototipo: `n8n.workflow.status.last[{#WORKFLOW_ID}]`
- Item prototipo (texto/JSON): `n8n.workflow.nodes.timing[{#WORKFLOW_ID}]`
- Trigger prototipo: error rate > umbral, duración > SLO, sin ejecuciones en N horas (heartbeat)

**Esto es lo que responde directamente al pedido de "garantizar que todo workflow creado tenga monitoreo"**: en cuanto un workflow nuevo aparece en n8n, la siguiente corrida del discovery lo detecta y Zabbix crea automáticamente sus items y triggers — sin que nadie tenga que configurar nada a mano por workflow.

### 4.2 Extracción de tiempos por ejecución y por nodo

Por cada ejecución nueva (polling a `/rest/executions?status=success,error` cada 30–60s, con checkpoint para no reprocesar):

1. `GET /rest/executions/{id}?includeData=true`.
2. Parsear `data.resultData.runData`: por nodo, `startTime`, `executionTime` (ms), y si hubo error.
3. Enviar a Zabbix vía **protocolo trapper (`zabbix_sender`)**:
   - `n8n.workflow.duration.last[<id>]` = duración total de la ejecución (ms).
   - `n8n.workflow.status.last[<id>]` = 0/1 (éxito/error).
   - `n8n.workflow.nodes.timing[<id>]` = **JSON con el detalle por nodo** de esa ejecución: `[{"node":"HTTP Request","type":"n8n-nodes-base.httpRequest","ms":842},{"node":"Postgres","type":"n8n-nodes-base.postgres","ms":120}, ...]`. Como item de tipo *Texto* en Zabbix, con historial guardado; Grafana lo puede renderizar en una tabla/heatmap desglosado (parseando el JSON en el panel, o vía Grafana transformations).
   - Para workflows críticos (marcados por tag `criticidad:alta`), adicionalmente crear **items por nodo individual** (`n8n.node.duration[<workflow_id>,<node_name>]`) vía un segundo nivel de LLD, para tener series históricas por nodo y no solo la última ejecución. Esto se limita a workflows críticos para controlar la cantidad de items (NVPS) en Zabbix.

### 4.3 Errores

Cuando una ejecución falla, además del punto anterior:
- Se dispara el **Error Workflow** nativo de n8n (asignado por template, ver §5), que llama a un webhook del bridge con el detalle del error.
- El bridge envía un trapper item `n8n.workflow.error.last[<id>]` con el mensaje/nodo que falló, para que Zabbix dispare el trigger correspondiente y Grafana lo muestre en el panel de errores.

---

## 5. Proceso de gobernanza: "garantizar" que todo workflow tenga monitoreo

Monitoreo por diseño, en dos capas independientes (para que ninguna falle silenciosamente):

1. **Capa automática (no depende de humanos)**: el discovery del bridge (§4.1) monitorea *todos* los workflows que existan en la instancia, se hayan creado como se hayan creado. Esto ya garantiza cobertura mínima (ejecuciones, duración, estado) sin intervención humana.
2. **Capa de calidad/enriquecimiento (requiere convención)**:
   - **Plantilla de workflow estándar** para creadores: Error Workflow asignado, tags obligatorios (`team`, `criticidad`, `env`), nombre con convención `EQUIPO-Proceso-vN`.
   - **Workflow de auditoría** (en n8n, corre cada X horas): recorre `/rest/workflows` y valida compliance (¿tiene Error Workflow?, ¿tiene tags?, ¿está activo sin ejecuciones hace 30 días = huérfano?). Envía el resultado como item trapper `n8n.governance.compliance_ratio` y detalle de no conformes, visible en un dashboard de Grafana y con trigger en Zabbix si el ratio cae de un umbral.
3. Con (1) ya cubierto automáticamente, (2) deja de ser un punto único de falla: si alguien se salta la plantilla, igual queda visible en el dashboard de cumplimiento, y el workflow igual tiene métricas básicas por el discovery.

---

## 6. Dashboards en Grafana (usando el datasource de Zabbix)

1. **Overview de la plataforma**: workflows activos (conteo de items descubiertos), ejecuciones/min, tasa de error global.
2. **Drilldown por workflow**: duración de las últimas N ejecuciones, tasa de éxito/error, tendencia.
3. **Rendimiento por nodo** (el pedido explícito): panel que parsea el JSON de `n8n.workflow.nodes.timing[...]` para mostrar barras de duración por nodo de la última ejecución, y para workflows críticos, series históricas por nodo (`n8n.node.duration[...]`).
4. **Errores**: tabla de últimas ejecuciones fallidas con nodo y mensaje de error (`n8n.workflow.error.last[...]`).
5. **Cumplimiento de gobernanza**: `n8n.governance.compliance_ratio` y lista de workflows no conformes.
6. **Problemas activos de Zabbix** (panel nativo del plugin): triggers disparados relacionados a n8n, con severidad y tiempo de ack — aprovecha el motor de alertas de Zabbix que la compañía ya usa.

El **alerting real** (umbrales, escalamiento, notificaciones) se define como **triggers en Zabbix**, no en Grafana, ya que Zabbix es la herramienta oficial para eso:
- Trigger por workflow: tasa de error > umbral en ventana de 15 min.
- Trigger por workflow crítico: duración > SLO definido.
- Trigger de heartbeat: workflow activo sin ejecuciones esperadas.
- Trigger de compliance: `compliance_ratio` bajo un piso.

---

## 7. Roadmap de implementación

| Fase | Contenido | Duración estimada |
|---|---|---|
| 0. Descubrimiento | Confirmar versión de Zabbix (¿soporta preprocesamiento Prometheus?), acceso de red n8n↔Zabbix, dónde correrá el bridge, inventario de workflows actuales | 3–5 días |
| 1. Métricas agregadas | Activar `/metrics` en n8n, crear item HTTP agent en Zabbix con preprocesamiento Prometheus, dashboard Overview en Grafana | 1 semana |
| 2. Bridge – discovery | Construir `n8n-zabbix-bridge` (parte de descubrimiento LLD), definir regla de discovery + prototipos de item/trigger en Zabbix | 1–2 semanas |
| 3. Bridge – ejecuciones y nodos | Agregar polling de ejecuciones, parseo de `runData`, envío trapper de duración/estado/timing por nodo | 2 semanas |
| 4. Dashboards de nodo/ejecución | Panel de rendimiento por nodo, panel de errores, en Grafana | 1 semana |
| 5. Gobernanza | Plantilla de workflow, workflow de auditoría/compliance, dashboard de cumplimiento | 1–2 semanas |
| 6. Alerting | Triggers en Zabbix (error rate, SLO, heartbeat, compliance) con escalamiento existente | 1 semana |
| 7. Rollout y documentación | Migrar workflows existentes a la plantilla, capacitar al equipo, publicar runbook | continuo |

---

## 8. Riesgos y decisiones a validar con el equipo

- **Versión de Zabbix**: el preprocesamiento "Prometheus pattern" para leer `/metrics` requiere Zabbix ≥ 6.0 — confirmar versión instalada.
- **Volumen de items (NVPS)**: el detalle por-nodo histórico (§4.2, segundo nivel de LLD) puede generar muchos items si se aplica a todos los workflows con muchos nodos; por eso se limita a workflows marcados como críticos. Ajustar el umbral según capacidad del Zabbix Server.
- **Frecuencia de polling del bridge**: si hay muy alto volumen de ejecuciones por minuto, evaluar bajar la granularidad (agregar antes de enviar) o aumentar el intervalo.
- **Credenciales**: el bridge necesita una API Key de n8n con permisos de lectura sobre workflows/ejecuciones y un token/host de Zabbix Sender; deben gestionarse como secretos.
- **Ubicación del bridge**: definir si corre como contenedor junto a n8n, como Zabbix Agent2 con plugin custom, o como workflow propio de n8n que llama a `zabbix_sender` vía nodo Execute Command — esta última opción evita construir un servicio externo, a costa de menos control de errores/reintentos.

---

## 9. Próximos pasos concretos

1. Confirmar versión de Zabbix y si el datasource de Zabbix para Grafana ya está instalado/configurado.
2. Activar `/metrics` en n8n y crear el primer item HTTP agent en Zabbix (Fase 1) como quick win, mientras se construye el bridge.
3. Diseñar y construir `n8n-zabbix-bridge` (Fases 2–3) como servicio versionado dentro de este repositorio (`graphn8n`), junto con la definición de la plantilla de Zabbix (template `.yaml`/XML exportable) y los dashboards de Grafana como código (JSON).
