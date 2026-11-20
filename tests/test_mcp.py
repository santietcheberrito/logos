"""Prueba el MCP Server de verdad: proceso aparte, por stdio y por HTTP con Bearer."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from logos import gobernanza as g

RAIZ = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _texto(res):
    assert res.content, res
    return res.content[0].text


def _datos(res):
    assert not res.is_error, _texto(res)
    return res.structured_content if res.structured_content is not None else json.loads(_texto(res))


def _tokens(empresa_id):
    return (g.crear_token(empresa_id, "Admin mcp", "admin"), g.crear_token(empresa_id, "Agente mcp", "agente"))


async def _flujo(session: ClientSession, session_admin: ClientSession):
    herramientas = {t.name for t in (await session.list_tools()).tools}
    assert {"buscar_contexto", "obtener_definicion", "obtener_proceso", "obtener_reglas",
            "proponer_actualizacion", "cargar_documento", "aprobar_propuesta"} <= herramientas

    r = _datos(await session.call_tool("proponer_actualizacion", {
        "tipo": "definicion", "contenido": {"termino": "Lead caliente", "definicion": "Pidió precio en 7 días"},
        "justificacion": "falta"}))
    pid = r["propuesta_id"]

    # el agente no puede aprobar
    res = await session.call_tool("aprobar_propuesta", {"propuesta_id": pid})
    assert res.is_error and "administradores" in _texto(res)

    assert _datos(await session.call_tool("obtener_definicion", {"termino": "lead caliente"}))["encontrada"] is False
    _datos(await session_admin.call_tool("aprobar_propuesta", {"propuesta_id": pid}))
    d = _datos(await session.call_tool("obtener_definicion", {"termino": "lead caliente"}))
    assert d["encontrada"] and d["definicion"]["definicion"] == "Pidió precio en 7 días"

    b = _datos(await session.call_tool("buscar_contexto", {"query": "lead caliente"}))
    assert b["resultados"][0]["titulo"] == "Lead caliente"


async def test_mcp_stdio(empresa):
    eid, _, _ = empresa
    admin, agente = _tokens(eid)
    env = {**os.environ, "PYTHONPATH": str(RAIZ)}

    def params(token):
        return StdioServerParameters(command=sys.executable, args=["-m", "logos.servidor_mcp"],
                                     env={**env, "LOGOS_TOKEN": token}, cwd=str(RAIZ))

    async with stdio_client(params(agente)) as (r1, w1), stdio_client(params(admin)) as (r2, w2):
        async with ClientSession(r1, w1) as s1, ClientSession(r2, w2) as s2:
            await s1.initialize()
            await s2.initialize()
            await _flujo(s1, s2)


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_mcp_http_con_bearer(empresa):
    eid, _, _ = empresa
    admin, agente = _tokens(eid)
    puerto = _puerto_libre()
    proc = subprocess.Popen([sys.executable, "-m", "logos.servidor_mcp", "--http", "--port", str(puerto)],
                            cwd=RAIZ, env={**os.environ, "PYTHONPATH": str(RAIZ)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", puerto), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        url = f"http://127.0.0.1:{puerto}/mcp"

        def cliente(token):
            return httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {})

        # sin token: error de acceso
        async with cliente(None) as h, streamable_http_client(url, http_client=h) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("buscar_contexto", {"query": "x"})
                assert res.is_error and "token" in _texto(res).lower()

        async with cliente(agente) as h1, streamable_http_client(url, http_client=h1) as (r1, w1), \
                cliente(admin) as h2, streamable_http_client(url, http_client=h2) as (r2, w2):
            async with ClientSession(r1, w1) as s1, ClientSession(r2, w2) as s2:
                await s1.initialize()
                await s2.initialize()
                await _flujo(s1, s2)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
