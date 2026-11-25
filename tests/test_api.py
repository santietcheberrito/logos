"""API REST: mismas reglas que el MCP (token, permisos, auditoría)."""
import pytest
from starlette.testclient import TestClient

from logos import db, gobernanza as g
from logos.servidor_mcp import mcp


@pytest.fixture
def api():
    with TestClient(mcp.streamable_http_app()) as c:
        yield c


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_api_sin_token_401(api):
    r = api.get("/api/buscar", params={"q": "x"})
    assert r.status_code == 401 and "error" in r.json()
    assert api.get("/api/buscar", params={"q": "x"}, headers=_h("lgs_agt_falso")).status_code == 401


def test_api_flujo_agente(api, empresa):
    eid, admin, _ = empresa
    agente = g.crear_token(eid, "Agente REST", "agente")
    token_admin = g.crear_token(eid, "Admin REST", "admin")

    r = api.post("/api/propuestas", headers=_h(agente), json={
        "tipo": "definicion", "contenido": {"termino": "Lead frío", "definicion": "Sin contacto en 60 días"},
        "justificacion": "falta"})
    assert r.status_code == 200 and r.json()["estado"] == "pendiente"
    assert api.get("/api/definicion", params={"termino": "lead frío"}, headers=_h(agente)).json()["encontrada"] is False

    g.aprobar(admin, r.json()["propuesta_id"])
    d = api.get("/api/definicion", params={"termino": "lead frío"}, headers=_h(agente)).json()
    assert d["encontrada"] and d["definicion"]["definicion"] == "Sin contacto en 60 días"

    b = api.post("/api/buscar", headers=_h(agente), json={"q": "lead frío", "limite": 3}).json()
    assert b["resultados"][0]["titulo"] == "Lead frío"
    assert api.get("/api/buscar", params={"q": "lead frío"}, headers=_h(agente)).json()["cantidad"] >= 1

    # el agente no puede exportar; el admin sí, en JSON y en Markdown
    assert api.get("/api/exportar", headers=_h(agente)).status_code == 403
    assert api.get("/api/exportar", headers=_h(token_admin)).json()["definicion"][0]["termino"] == "Lead frío"
    md = api.get("/api/exportar", params={"formato": "markdown"}, headers=_h(token_admin))
    assert md.headers["content-type"].startswith("text/markdown") and "### Lead frío" in md.text

    # las consultas por REST quedan auditadas, marcadas como canal rest
    with db.conexion() as conn:
        n = conn.execute("""select count(*) n from auditoria where empresa_id = %s and accion = 'buscar_contexto'
                            and detalle->'params'->>'canal' = 'rest'""", (eid,)).fetchone()["n"]
    assert n == 2


def test_api_datos_invalidos_400(api, empresa):
    eid, _, _ = empresa
    agente = g.crear_token(eid, "Agente REST", "agente")
    assert api.get("/api/buscar", headers=_h(agente)).status_code == 400
    assert api.get("/api/buscar", params={"q": "x", "tipo": "nada"}, headers=_h(agente)).status_code == 400
    assert api.post("/api/propuestas", headers=_h(agente), content="no es json").status_code == 400
    r = api.post("/api/propuestas", headers=_h(agente), json={"tipo": "regla", "contenido": {"nombre": "x"},
                                                               "justificacion": ""})
    assert r.status_code == 400 and "justificación" in r.json()["error"]
