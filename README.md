# Logos

Logos es la capa de contexto oficial de una empresa para sus agentes de IA. Los agentes **consultan** Logos por MCP
(no improvisan) y solo pueden **proponer** cambios; una persona los aprueba. Todo queda versionado
y auditado. Multi-tenant desde el día uno (`empresa_id` en todas las tablas).

```
Agentes (Claude, GPT, propios)
        ↓  MCP (stdio o HTTP + Bearer token)
MCP Server  — logos/servidor_mcp.py
        ↓  conexión de backend (salta RLS, nunca sale del servidor)
Supabase  — Postgres + pgvector + pg_trgm + RLS
        ↑
Dashboard de administración — dashboard/app.py
```

## Cómo funciona la gobernanza

| Quién | Puede |
|---|---|
| Agente (token `lgs_agt_…`) | leer lo **aprobado**, proponer cambios, cargar documentos en **borrador** |
| Admin (token `lgs_adm_…`) | además: listar, aprobar y rechazar propuestas |

- La empresa sale **del token**, nunca de lo que mande el agente. Un token no ve nada de otra empresa.
- Ningún agente escribe en `definiciones`, `procesos`, `reglas` ni `documentos`: todo entra por `propuestas`.
- Al aprobar: se crea la versión `n+1` en estado `aprobado` y la anterior pasa a `obsoleto`
  (un índice único impide dos versiones aprobadas del mismo término).
- En procesos y reglas se puede proponer solo lo que cambia; el resto se hereda de la versión vigente.
- `"accion": "obsoletar"` en el contenido pide dar de baja algo.
- `auditoria` registra cada consulta y cada cambio. Un trigger impide editarla o borrarla.
- RLS activo en todas las tablas: `anon` no ve nada; un usuario de Supabase Auth con
  `app_metadata = {empresa_id, rol}` lee solo lo aprobado de su empresa (admin ve borradores y
  propuestas) y no puede escribir directo. Los tokens son invisibles fuera del backend.

## Tools del MCP

| Tool | Para qué |
|---|---|
| `buscar_contexto(query, tipo="general", limite=5)` | Búsqueda híbrida: exacta/trigramas en definiciones, procesos y reglas + semántica y texto completo en documentos (fusión RRF). `tipo`: general, definiciones, procesos, reglas, documentos |
| `obtener_definicion(termino)` | Definición vigente exacta; si no existe, sugiere términos parecidos y avisa que no improvise |
| `obtener_proceso(nombre)` | Proceso con pasos, responsable y restricciones |
| `obtener_reglas(aplica_a)` | Reglas del área + las generales (`aplica_a = "todos"`) |
| `proponer_actualizacion(tipo, contenido, justificacion)` | Crea una propuesta pendiente |
| `cargar_documento(titulo, contenido, tipo, fuente)` | Limpia, divide (700 tokens, solapamiento 100), genera embeddings y guarda en borrador + propuesta |
| `listar_propuestas(estado)` | Solo admin |
| `aprobar_propuesta(propuesta_id)` | Solo admin. Aplica y versiona |
| `rechazar_propuesta(propuesta_id, motivo)` | Solo admin |

Cada tool tiene su descripción pensada para que el agente sepa cuándo usarla, y el servidor manda
instrucciones generales ("consultá antes de responder, no improvises, las reglas son obligatorias").

## Instalar en un negocio

1. **Supabase**: crear el proyecto (región São Paulo para Argentina) y correr
   `supabase/migrations/20261120000000_logos.sql` en el SQL Editor (o `supabase db push`).
2. **Configurar** `.env` a partir de `.env.example`:
   `DATABASE_URL` (Project Settings → Database → connection string, *session pooler*) y `OPENAI_API_KEY`.
3. **Instalar** dependencias:
   ```bash
   /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
   ```
4. **Crear la empresa** (muestra el token admin y uno de agente, una sola vez):
   ```bash
   .venv/bin/python -m logos.cli crear-empresa "Nombre del negocio"
   ```
   Más tokens: `python -m logos.cli crear-token EMPRESA_ID "Agente de ventas" agente`
5. **Cargar conocimiento**: documentos con `python -m logos.cli cargar archivo.pdf --token TOKEN --tipo SOP`
   o desde el dashboard; definiciones/procesos/reglas como propuestas. Después aprobar en el dashboard.
6. **Conectar agentes** (abajo).

## Conectar un agente

**Claude Desktop / Cursor (stdio)** — en la config de MCP:
```json
{
  "mcpServers": {
    "logos": {
      "command": "/ruta/a/BRAIN SYSTEM/.venv/bin/python",
      "args": ["-m", "logos.servidor_mcp"],
      "cwd": "/ruta/a/BRAIN SYSTEM",
      "env": { "LOGOS_TOKEN": "lgs_agt_..." }
    }
  }
}
```

**Remoto (HTTP)** — `python -m logos.servidor_mcp --http --port 4912` (o el `Dockerfile` en
Railway/Fly con `HOST=0.0.0.0`). Endpoint `https://<host>/mcp`, chequeo de vida en `/salud`.
Cada agente manda `Authorization: Bearer <token>`.

**Claude Code**:
```bash
claude mcp add --transport http logos https://<host>/mcp --header "Authorization: Bearer lgs_agt_..."
```

## Dashboard

```bash
.venv/bin/python -m dashboard.app        # http://127.0.0.1:4910, entrar con token admin
```
- **Inicio**: propuestas para revisar (con lo que cambia frente a la versión vigente) y, al costado,
  lo que los agentes buscaron y no encontraron en los últimos 7 días.
- **Conocimiento**: lo que hoy es oficial. **Cargar documento**: .md, .txt, .pdf o texto pegado.
- **Actividad**: el registro de auditoría.

Detrás de HTTPS poner `DASHBOARD_HTTPS=1` (cookie segura).

## Entorno local de prueba

```bash
scripts/base_local.sh           # Postgres 17 + pgvector en 127.0.0.1:55440 (reset: scripts/base_local.sh reset)
.venv/bin/python -m pytest -q   # 18 pruebas: gobernanza, versionado, aislamiento, RLS, MCP stdio y HTTP
```
`scripts/supabase_local_shim.sql` imita los roles y `auth.jwt()` de Supabase para probar RLS; no
se aplica en Supabase. Con `EMBEDDINGS_PROVIDER=local` los embeddings son un hashing de palabras sin
red: sirve para probar el flujo, no para producción.

Datos de ejemplo (empresa ficticia):
```bash
.venv/bin/python -m logos.cli ejemplos --token TOKEN_ADMIN
```

## Decisiones respecto del brief

- `dueño` → `dueno` (identificadores ASCII; la tool acepta las dos formas).
- Se agregaron `empresas`, `tokens_acceso` y `auditoria`; `version`/`actualizado_en` en todas las
  tablas de conocimiento; `reglas` admite `obsoleto`; `chunks` lleva `orden` y un `tsvector` en español.
- Las propuestas son el "borrador" de definiciones, procesos y reglas; los documentos sí se guardan en
  borrador porque hay que indexarlos antes de aprobarlos.
- Voyage no está implementado: sus modelos no dan 1536 dimensiones; usarlo implica cambiar
  `vector(1536)` y `EMBEDDINGS_DIM`.
- Si OpenAI falla al cargar, el documento se guarda igual sin embeddings (la búsqueda por texto
  funciona) y `python -m logos.cli reindexar` los completa.
