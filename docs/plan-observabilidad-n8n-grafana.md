# Plan de Observabilidad n8n → Grafana

## 1. Objetivo

Garantizar que **todo** workflow creado en n8n quede automáticamente monitoreado, con visibilidad de:

- Estado de cada ejecución (éxito / error / esperando / cancelada).
- Tiempo de respuesta total de la ejecución.
- Tiempo de respuesta **por nodo** dentro de cada ejecución.
- Errores, reintentos y causa raíz, correlacionados con logs.
- Salud de la plataforma n8n (colas, workers, uso de recursos).

Todo esto centralizado en Grafana como herramienta oficial, sin depender de que cada creador de workflow "se acuerde" de instrumentar nada.

> **Nota importante sobre la edición de n8n**: parte de las fuentes de datos más ricas (Log Streaming / Event Bus con eventos por nodo) son funcionalidad **Enterprise**. Este plan está diseñado para funcionar en **Community Edition** como base obligatoria, y señala dónde Enterprise mejora la granularidad. Antes de ejecutar, confirmar qué licencia se usa y si ya existe stack Prometheus/Loki/Tempo corporativo o hay que desplegarlo.

---

## 2. Fuentes de datos disponibles en n8n

| Fuente | Qué entrega | Nivel de detalle | Disponibilidad |
|---|---|---|---|
| **Endpoint `/metrics` (Prometheus)** | Contadores/histogramas agregados: ejecuciones por estado, duración, cache, colas (modo queue), endpoints de la API | Agregado (no por ejecución individual) | Community (activar con env vars) |
| **REST API `/executions` y `/executions/{id}`** | Detalle completo de cada ejecución, incluyendo `resultData.runData` con `startTime` y `executionTime` (ms) por nodo | Por ejecución y por nodo | Community |
| **Error Workflow** (Settings → Error Workflow) | Se dispara un workflow cuando otro falla, con contexto del error | Por ejecución fallida | Community |
| **Logs de n8n (stdout/archivo)** | Logs estructurados si se configura `N8N_LOG_FORMAT=json` | Nivel proceso, no siempre por nodo | Community |
| **Log Streaming / Event Bus** (`n8n.workflow.started/success/failed`, `n8n.node.started/finished`) | Eventos en tiempo real por nodo, enviables a webhook/Sentry/syslog | Por nodo, en tiempo real | **Enterprise** |
| **Modo queue (Redis + workers)** | Métricas de profundidad de cola, jobs activos/fallidos | Infraestructura | Community (si se usa scaling mode) |

**Conclusión clave**: para tener tiempos por nodo de forma confiable en Community Edition, la fuente de verdad es `runData` dentro de cada ejecución vía API REST — no el endpoint de métricas (que es agregado y no expone por-nodo por diseño, para evitar explosión de cardinalidad en Prometheus).

---

## 3. Arquitectura propuesta

```
┌─────────────┐   scrape /metrics   ┌────────────┐
│   n8n        │ ───────────────────▶│ Prometheus │
│ (instancia)  │                     └─────┬──────┘
│              │   logs JSON (stdout)      │
│              │ ─────┐                    │
│              │      ▼                    │
│              │  ┌──────────┐             │
│              │  │ Vector / │             │
│              │  │ Promtail │──▶ Loki ─────┤
│              │  └──────────┘             │
│              │                           │
│   REST API   │◀── polling cada N min ────┤
│  /executions │       │                   │
└──────┬───────┘       ▼                   │
       │        ┌──────────────────┐       │
       │        │ Exporter/Colector │       │
       │        │ (servicio propio) │       │
       │        └───┬─────────┬────┘       │
       │            │         │            │
       │   métricas │         │ spans OTLP │
       │  Pushgateway│        ▼            │
       │            │    ┌────────┐        │
       │            │    │ Tempo  │        │
       │            ▼    └───┬────┘        │
       │        Prometheus   │             │
       │                     │             │
       │                     ▼             ▼
       │                 ┌─────────────────────┐
       └────────────────▶│      Grafana         │
      Error Workflow ────▶│ Dashboards + Alerting│
      (alertas directas)  └─────────────────────┘
```

Tres capas de datos complementarias, no excluyentes:

1. **Prometheus (métricas agregadas)** → salud general, tasas, SLOs.
2. **Loki (logs)** → búsqueda y correlación de errores/eventos puntuales.
3. **Tempo (trazas)** → la pieza que responde exactamente a "tiempo de respuesta por nodo": cada ejecución de workflow = 1 traza, cada nodo = 1 span, visualizado como waterfall en Grafana.

---

## 4. Componente central: el "Exporter" de ejecuciones

Es la pieza que no trae n8n de fábrica y que resuelve el requisito de "tiempos por nodo". Es un servicio pequeño (Node.js o Python) que:

1. **Se entera de ejecuciones terminadas** por una de dos vías:
   - Community: polling periódico a `GET /rest/executions?status=success,error&limit=...` (cada 30–60s) usando API Key de n8n.
   - Enterprise: se suscribe al Event Bus (webhook) y recibe `n8n.workflow.success` / `n8n.workflow.failed` en tiempo real, sin polling.
2. **Obtiene el detalle completo** con `GET /rest/executions/{id}?includeData=true`.
3. **Parsea `data.resultData.runData`**: por cada nodo obtiene `startTime`, `executionTime`, `executionStatus`, y si hubo error.
4. **Emite la información en 3 formatos en paralelo**:
   - **Traza OTLP a Tempo**: workflow = trace, nodos = spans (root span = ejecución completa). Esto da la vista de "cascada" por ejecución que se pide.
   - **Log estructurado a stdout** (recogido por Vector → Loki) con campos: `workflow_id`, `workflow_name`, `execution_id`, `node_name`, `node_type`, `duration_ms`, `status`. Permite búsquedas tipo "nodos más lentos de la última hora".
   - **Métrica agregada a Prometheus** (vía Pushgateway o exposición propia `/metrics`) tipo histograma: `n8n_node_execution_duration_seconds{workflow_name, node_type, status}` — **sin** incluir `execution_id` ni `node_name` libre como label, para no explotar cardinalidad; usar `node_type` (genérico) y opcionalmente `workflow_name` si el número de workflows es manejable.
5. Guarda cursor/checkpoint (última ejecución procesada) para no reprocesar.

Este exporter es la única pieza de infraestructura nueva que hay que construir; todo lo demás es configuración de componentes existentes (n8n, Prometheus, Loki, Tempo, Grafana).

---

## 5. Proceso de gobernanza: "garantizar" que todo workflow tenga monitoreo

Monitoreo por diseño, no por convención:

1. **Error Workflow obligatorio a nivel instancia/proyecto**: configurar un workflow de error por defecto (en n8n Enterprise esto se hereda por Proyecto; en Community hay que fijarlo manualmente por workflow o vía script de auditoría — ver punto 3).
2. **Workflow "plantilla" estándar**: toda creación nueva parte de una plantilla que ya trae:
   - Nodo `Error Trigger` conectado / Error Workflow asignado.
   - Tags obligatorios: `team:<equipo>`, `criticidad:<alta|media|baja>`, `env:<prod|staging>`.
   - Nombre siguiendo convención `EQUIPO-Proceso-vN`.
3. **Workflow de auditoría (meta-monitoreo)**: un workflow propio en n8n, corriendo cada X horas, que llama a la API `GET /workflows` y verifica compliance:
   - ¿Tiene Error Workflow asignado? ¿Tiene tags? ¿Está activo pero sin ejecuciones en 30 días (huérfano)?
   - Si algo falla, publica una **anotación en Grafana** (API `/api/annotations`) y una alerta en Slack/Teams con el link al workflow no conforme.
   - Este mismo workflow expone su resultado como métrica `n8n_governance_compliance_ratio` para dashboard de cumplimiento.
4. **Si hay control de versiones de workflows (n8n Git integration / Source Control - Enterprise)**: agregar un check en el pipeline de CI que valide el JSON del workflow (¿tiene errorWorkflow seteado? ¿tiene nodos sin nombre?) antes de mergear a producción.
5. **Onboarding**: checklist en la documentación interna (ver skill `doc-asistente-metria` para publicarla) que todo creador de workflow debe seguir, más el punto 3 como red de seguridad automática (no depende de que la gente lea la checklist).

---

## 6. Dashboards en Grafana (propuesta de carpeta)

1. **Overview de la plataforma**: workflows activos, ejecuciones/min, tasa de error global, uso de colas/workers.
2. **Drilldown por workflow**: duración p50/p90/p99, tasa de éxito/error, tendencia en el tiempo, últimas ejecuciones fallidas (link a logs en Loki).
3. **Rendimiento por nodo** (el pedido explícito del usuario): tabla/heatmap de nodos más lentos por tipo (`HTTP Request`, `Function`, `Postgres`, etc.), y para una ejecución puntual, el waterfall de Tempo mostrando cuánto tardó cada nodo.
4. **Errores y causa raíz**: panel de logs (Loki) filtrable por workflow/nodo/mensaje de error, correlacionado con las trazas.
5. **Cumplimiento de gobernanza**: % de workflows con Error Workflow asignado, tags correctos, sin ejecuciones huérfanas.
6. **Salud de infraestructura n8n**: CPU/memoria del proceso, profundidad de cola Redis, workers activos (si hay scaling mode).

Alerting en Grafana (no solo dashboards):
- Tasa de error de un workflow > umbral en 15 min → Slack/PagerDuty/Grafana OnCall.
- Duración p95 de un workflow crítico supera su SLO → alerta.
- Workflow crítico sin ejecuciones esperadas (heartbeat) → alerta de "silencio".
- Cola de ejecuciones creciendo sostenidamente (backlog) → alerta de saturación.

---

## 7. Roadmap de implementación

| Fase | Contenido | Duración estimada |
|---|---|---|
| 0. Descubrimiento | Confirmar edición n8n (Community/Enterprise), modo de despliegue (single/queue), stack Grafana existente (¿ya hay Prometheus/Loki/Tempo?), inventario de workflows actuales | 3–5 días |
| 1. Métricas base | Activar `/metrics` de n8n, desplegar/ajustar Prometheus, dashboard Overview | 1 semana |
| 2. Logs | Configurar logging JSON en n8n, pipeline Vector/Promtail → Loki, dashboard de errores | 1 semana |
| 3. Exporter de ejecuciones | Construir el servicio descrito en §4, con salida a Tempo + Loki + Prometheus | 2–3 semanas |
| 4. Dashboards de nodo/ejecución | Waterfall por ejecución, ranking de nodos lentos | 1 semana |
| 5. Gobernanza | Plantilla de workflow, Error Workflow por defecto, workflow de auditoría/compliance | 1–2 semanas |
| 6. Alerting y SLOs | Definir SLOs por criticidad de workflow, reglas de alerta en Grafana | 1 semana |
| 7. Rollout y documentación | Migrar workflows existentes a la plantilla, capacitar al equipo, publicar runbook | continuo |

---

## 8. Riesgos y decisiones a validar con el equipo

- **Cardinalidad en Prometheus**: no usar `execution_id` ni nombres de nodo libres como labels; usar `node_type` + `workflow_name` (o `workflow_id` si los nombres cambian).
- **Volumen del exporter**: si hay miles de ejecuciones/minuto, el polling a la API REST puede no escalar → en ese caso, priorizar Enterprise + Event Bus, o usar el propio `Error Trigger`/`Execute Workflow` para emitir eventos en push en vez de poll.
- **Retención**: definir cuánto tiempo se guardan trazas (Tempo) y logs (Loki) de ejecuciones — puede ser costoso si son muchos workflows.
- **Seguridad**: la API Key del exporter necesita permisos de lectura sobre ejecuciones; debe manejarse como secreto (no hardcodeado).

---

## 9. Próximos pasos concretos

1. Confirmar con el equipo: ¿Community o Enterprise?, ¿ya existe Prometheus/Loki/Tempo en la compañía o hay que desplegarlos?, ¿n8n corre en modo single o queue?
2. Con esas respuestas, priorizar Fase 0 y Fase 1 (quick win: dashboard Overview con métricas nativas de n8n, sin necesidad de construir nada).
3. Diseñar el exporter (Fase 3) como repo/servicio separado dentro de este mismo repositorio (`graphn8n`), versionado junto con los dashboards de Grafana como código (JSON/`grafonnet`).
