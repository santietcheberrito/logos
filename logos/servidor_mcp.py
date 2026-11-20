"""MCP Server de Logos.

Uso:
  stdio (Claude Desktop, Cursor):  LOGOS_TOKEN=lgs_agt_... python -m logos.servidor_mcp
  HTTP (Railway / Fly):            python -m logos.servidor_mcp --http --port 4912
                                   y cada agente manda  Authorization: Bearer <token>
"""
from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config, gobernanza, lectura
from .gobernanza import AccesoDenegado, DatosInvalidos, Identidad

INSTRUCCIONES = """\
Este servidor es Logos, el conocimiento oficial de la empresa: definiciones, procesos, reglas y documentos
APROBADOS por la empresa. Reglas para el agente:
1. Antes de responder sobre la empresa (qué significa un término, cómo se hace algo, qué está
   permitido), consultá Logos. No improvises ni completes con conocimiento general.
2. Antes de ejecutar una acción, revisá obtener_reglas para tu área: las reglas son obligatorias.
3. Si la información no está, decilo. Si detectás algo faltante o desactualizado, usá
   proponer_actualizacion: un humano la revisa. Nunca asumas que tu propuesta ya rige.
"""

LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                          open_world_hint=False)
ESCRITURA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                            open_world_hint=False)

mcp = MCPServer(name="logos", title="Logos", instructions=INSTRUCCIONES,
                version="0.1.0")

_HTTP = False


def _identidad(ctx: Context) -> Identidad:
    token = None
    if _HTTP:
        headers = ctx.headers or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    else:
        token = config.LOGOS_TOKEN
    try:
        return gobernanza.identificar(token)
    except AccesoDenegado as e:
        raise ToolError(str(e)) from e


def _ejecutar(ctx: Context, accion: str, params: dict[str, Any], fn, *args: Any) -> dict[str, Any]:
    ident = _identidad(ctx)
    try:
        resultado = fn(ident, *args)
    except (AccesoDenegado, DatosInvalidos) as e:
        gobernanza.auditar(ident, accion, {"params": params, "error": str(e)})
        raise ToolError(str(e)) from e
    if accion.startswith(("buscar", "obtener")):  # las escrituras ya se auditan en su transacción
        cantidad = resultado.get("cantidad", 1 if (resultado.get("encontrada") or resultado.get("encontrado")) else 0)
        gobernanza.auditar(ident, accion, {"params": params, "resultados": cantidad,
                                           "devueltos": _devueltos(accion, resultado)})
    return resultado


def _devueltos(accion: str, resultado: dict[str, Any]) -> list[dict[str, Any]]:
    """Qué devolvió la consulta (para saber qué se consulta más). Sin el contenido."""
    if accion == "buscar_contexto":
        salida = []
        for r in resultado.get("resultados", []):
            rid = r["detalle"]["documento_id"] if r["tipo"] == "documento" else r["id"]
            salida.append({"tipo": r["tipo"], "id": rid, "titulo": r["titulo"]})
        return salida
    if accion == "obtener_definicion" and resultado.get("encontrada"):
        d = resultado["definicion"]
        return [{"tipo": "definicion", "id": d["id"], "titulo": d["termino"]}]
    if accion == "obtener_proceso" and resultado.get("encontrado"):
        p = resultado["proceso"]
        return [{"tipo": "proceso", "id": p["id"], "titulo": p["nombre"]}]
    if accion == "obtener_reglas":
        return [{"tipo": "regla", "id": r["id"], "titulo": r["nombre"]} for r in resultado.get("reglas", [])]
    return []


# ------------------------------------------------------------------ lectura

@mcp.tool(annotations=LECTURA)
def buscar_contexto(ctx: Context, query: str, tipo: str = "general", limite: int = 5) -> dict[str, Any]:
    """Busca en el conocimiento oficial de la empresa (solo contenido aprobado).

    Usalo cuando necesites contexto para responder una pregunta sobre la empresa y no sabés
    en qué definición, proceso o documento está. Combina búsqueda exacta en definiciones,
    procesos y reglas con búsqueda semántica en documentos (SOPs, políticas, FAQs, manuales).

    Args:
        query: la pregunta o tema en lenguaje natural, ej. "cómo se pide una devolución".
        tipo: "general" (todo), "definiciones", "procesos", "reglas" o "documentos".
        limite: cantidad máxima de resultados (1 a 20).

    Si no hay resultados, NO inventes la respuesta: decí que no está en Logos.
    """
    return _ejecutar(ctx, "buscar_contexto", {"query": query, "tipo": tipo, "limite": limite},
                     lectura.buscar_contexto, query, tipo, limite)


@mcp.tool(annotations=LECTURA)
def obtener_definicion(ctx: Context, termino: str) -> dict[str, Any]:
    """Devuelve la definición oficial y vigente de un término de la empresa.

    Usalo cuando aparezca un término propio del negocio (ej. "cliente activo", "MRR",
    "pedido urgente") y necesites su significado exacto. Si no existe, devuelve términos
    parecidos como sugerencia; no uses una definición propia en su lugar.
    """
    return _ejecutar(ctx, "obtener_definicion", {"termino": termino}, lectura.obtener_definicion, termino)


@mcp.tool(annotations=LECTURA)
def obtener_proceso(ctx: Context, nombre: str) -> dict[str, Any]:
    """Devuelve un proceso oficial completo: descripción, pasos, responsable y restricciones.

    Usalo cuando tengas que explicar o ejecutar cómo se hace algo en la empresa
    (ej. "alta de cliente", "devolución", "cierre de caja"). Seguí los pasos en orden.
    """
    return _ejecutar(ctx, "obtener_proceso", {"nombre": nombre}, lectura.obtener_proceso, nombre)


@mcp.tool(annotations=LECTURA)
def obtener_reglas(ctx: Context, aplica_a: str) -> dict[str, Any]:
    """Devuelve las reglas de negocio obligatorias para un área, más las generales.

    Usalo ANTES de actuar o prometer algo a un cliente. `aplica_a` es el área o tipo de
    agente: "agentes", "ventas", "soporte", "cobranzas", etc. Las reglas marcadas para
    "todos" se incluyen siempre. Si una acción viola una regla, no la hagas.
    """
    return _ejecutar(ctx, "obtener_reglas", {"aplica_a": aplica_a}, lectura.obtener_reglas, aplica_a)


# ------------------------------------------------------------------ escritura controlada

@mcp.tool(annotations=ESCRITURA)
def proponer_actualizacion(ctx: Context, tipo: str, contenido: dict[str, Any], justificacion: str) -> dict[str, Any]:
    """Propone crear, cambiar o dar de baja conocimiento oficial. NO lo cambia: queda pendiente
    hasta que un administrador humano lo apruebe.

    Usalo cuando detectes algo faltante, desactualizado o contradictorio.

    Args:
        tipo: "definicion", "proceso", "regla" o "documento".
        contenido: según el tipo (agregá "accion": "obsoletar" para pedir una baja):
          - definicion: {"termino", "definicion", "categoria"?, "dueno"?}
          - proceso:    {"nombre", "descripcion"?, "pasos"? (lista), "responsable"?, "restricciones"?}
                        (si el proceso existe podés mandar solo los campos que cambian)
          - regla:      {"nombre", "aplica_a", "restriccion", "descripcion"?}
          - documento:  {"documento_id"} de un borrador, o {"titulo", "accion": "obsoletar"}
        justificacion: por qué hace falta el cambio y de dónde sale (obligatoria).
    """
    return _ejecutar(ctx, "proponer_actualizacion", {"tipo": tipo}, gobernanza.proponer,
                     tipo, contenido, justificacion)


@mcp.tool(annotations=ESCRITURA)
def cargar_documento(ctx: Context, titulo: str, contenido: str, tipo: str = "documento",
                     fuente: str = "") -> dict[str, Any]:
    """Carga un documento (SOP, política, FAQ, manual) en BORRADOR para que lo aprueben.

    El texto se limpia, se divide en fragmentos y se indexa para búsqueda semántica, pero
    no aparece en ninguna búsqueda hasta que un administrador lo apruebe. Si ya existe un
    documento aprobado con el mismo título, al aprobarse este lo reemplaza (nueva versión).

    Args:
        titulo: nombre del documento, ej. "Política de devoluciones".
        contenido: el texto completo.
        tipo: SOP, política, FAQ, manual, etc.
        fuente: de dónde sale (URL, archivo, persona).
    """
    return _ejecutar(ctx, "cargar_documento", {"titulo": titulo, "tipo": tipo}, gobernanza.cargar_documento,
                     titulo, contenido, tipo, fuente or None)


# ------------------------------------------------------------------ solo admin

@mcp.tool(annotations=LECTURA)
def listar_propuestas(ctx: Context, estado: str = "pendiente") -> list[dict[str, Any]]:
    """[Solo administradores] Lista propuestas de cambio: "pendiente", "aprobada", "rechazada" o "todas"."""
    ident = _identidad(ctx)
    try:
        return gobernanza.listar_propuestas(ident, estado)
    except (AccesoDenegado, DatosInvalidos) as e:
        gobernanza.auditar(ident, "listar_propuestas", {"error": str(e)})
        raise ToolError(str(e)) from e


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def aprobar_propuesta(ctx: Context, propuesta_id: str) -> dict[str, Any]:
    """[Solo administradores] Aprueba una propuesta: pasa a ser conocimiento oficial.

    Si ya había una versión aprobada, se crea una versión nueva y la anterior queda obsoleta.
    Un agente normal recibe un error: solo puede proponer.
    """
    return _ejecutar(ctx, "aprobar_propuesta", {"propuesta_id": propuesta_id}, gobernanza.aprobar, propuesta_id)


@mcp.tool(annotations=ESCRITURA)
def rechazar_propuesta(ctx: Context, propuesta_id: str, motivo: str) -> dict[str, Any]:
    """[Solo administradores] Rechaza una propuesta, dejando el motivo registrado."""
    return _ejecutar(ctx, "rechazar_propuesta", {"propuesta_id": propuesta_id}, gobernanza.rechazar,
                     propuesta_id, motivo)


@mcp.custom_route("/salud", methods=["GET"], include_in_schema=False)
async def salud(_request):
    """Chequeo de vida para Railway/Fly (no expone datos)."""
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True})


def main() -> None:
    global _HTTP
    parser = argparse.ArgumentParser(description="MCP Server de Logos")
    parser.add_argument("--http", action="store_true", help="Servir por HTTP (streamable) en vez de stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "4912")))
    args = parser.parse_args()
    if args.http:
        _HTTP = True
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
