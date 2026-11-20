import json

import psycopg
import pytest

from logos import chunking, db, gobernanza as g, lectura as l


def aprobar_nueva(admin, agente, tipo, contenido):
    p = g.proponer(agente, tipo, contenido, "prueba")
    return g.aprobar(admin, p["propuesta_id"])


def test_agente_no_puede_aprobar(empresa):
    _, admin, agente = empresa
    p = g.proponer(agente, "definicion", {"termino": "MRR", "definicion": "Ingreso mensual recurrente"}, "falta")
    with pytest.raises(g.AccesoDenegado):
        g.aprobar(agente, p["propuesta_id"])
    with pytest.raises(g.AccesoDenegado):
        g.rechazar(agente, p["propuesta_id"], "no")
    with pytest.raises(g.AccesoDenegado):
        g.listar_propuestas(agente)


def test_propuesta_no_rige_hasta_aprobarse(empresa):
    _, admin, agente = empresa
    p = g.proponer(agente, "definicion", {"termino": "Churn", "definicion": "Clientes que se van"}, "falta")
    assert l.obtener_definicion(agente, "churn")["encontrada"] is False
    g.aprobar(admin, p["propuesta_id"])
    r = l.obtener_definicion(agente, "CHURN")
    assert r["encontrada"] and r["definicion"]["version"] == 1


def test_versionado_obsoleta_la_anterior(empresa):
    eid, admin, agente = empresa
    aprobar_nueva(admin, agente, "definicion", {"termino": "Ticket", "definicion": "v1"})
    r = aprobar_nueva(admin, agente, "definicion", {"termino": "ticket", "definicion": "v2"})
    assert r["version"] == 2 and r["accion"] == "nueva_version"
    vigente = l.obtener_definicion(agente, "Ticket")["definicion"]
    assert vigente["definicion"] == "v2" and vigente["termino"] == "Ticket"
    with db.conexion() as conn:
        estados = conn.execute("select version, estado from definiciones where empresa_id = %s order by version",
                               (eid,)).fetchall()
    assert [(e["version"], e["estado"]) for e in estados] == [(1, "obsoleto"), (2, "aprobado")]


def test_proceso_cambio_parcial_hereda_campos(empresa):
    _, admin, agente = empresa
    aprobar_nueva(admin, agente, "proceso", {"nombre": "Cierre de caja", "descripcion": "Al final del día",
                                             "pasos": ["contar", "anotar"], "responsable": "Cajero"})
    r = aprobar_nueva(admin, agente, "proceso", {"nombre": "Cierre de caja", "pasos": ["contar", "anotar", "firmar"]})
    assert r["version"] == 2
    p = l.obtener_proceso(agente, "cierre de caja")["proceso"]
    assert p["pasos"] == ["contar", "anotar", "firmar"] and p["responsable"] == "Cajero"


def test_obsoletar_regla(empresa):
    _, admin, agente = empresa
    aprobar_nueva(admin, agente, "regla", {"nombre": "Sin envíos domingo", "aplica_a": "ventas",
                                           "restriccion": "No se envía los domingos"})
    assert l.obtener_reglas(agente, "ventas")["cantidad"] == 1
    aprobar_nueva(admin, agente, "regla", {"nombre": "Sin envíos domingo", "accion": "obsoletar"})
    assert l.obtener_reglas(agente, "ventas")["cantidad"] == 0


def test_reglas_generales_y_por_area(empresa):
    _, admin, agente = empresa
    aprobar_nueva(admin, agente, "regla", {"nombre": "A", "aplica_a": "ventas, soporte", "restriccion": "x"})
    aprobar_nueva(admin, agente, "regla", {"nombre": "B", "aplica_a": "todos", "restriccion": "y"})
    aprobar_nueva(admin, agente, "regla", {"nombre": "C", "aplica_a": "cobranzas", "restriccion": "z"})
    assert {r["nombre"] for r in l.obtener_reglas(agente, "soporte")["reglas"]} == {"A", "B"}


def test_documento_borrador_invisible_hasta_aprobar(empresa):
    _, admin, agente = empresa
    r = g.cargar_documento(agente, "Manual de garantías", "La garantía de los compresores es de 12 meses.",
                           "manual", "test")
    assert r["estado"] == "borrador" and r["chunks"] == 1
    assert l.buscar_contexto(agente, "garantía compresores", "documentos")["cantidad"] == 0
    g.aprobar(admin, r["propuesta_id"])
    res = l.buscar_contexto(agente, "garantía compresores", "documentos")
    assert res["cantidad"] == 1 and res["resultados"][0]["titulo"] == "Manual de garantías"


def test_documento_nueva_version_reemplaza(empresa):
    _, admin, agente = empresa
    r1 = g.cargar_documento(admin, "FAQ", "El horario es de 9 a 17.", "FAQ")
    g.aprobar(admin, r1["propuesta_id"])
    r2 = g.cargar_documento(admin, "FAQ", "El horario es de 8 a 18.", "FAQ")
    a = g.aprobar(admin, r2["propuesta_id"])
    assert a["version"] == 2 and a["reemplaza"] == r1["documento_id"]
    res = l.buscar_contexto(agente, "horario", "documentos")
    assert [x["contenido"] for x in res["resultados"]] == ["El horario es de 8 a 18."]


def test_rechazo_deja_motivo(empresa):
    _, admin, agente = empresa
    r = g.cargar_documento(agente, "Borrador raro", "contenido dudoso")
    g.rechazar(admin, r["propuesta_id"], "no es oficial")
    with pytest.raises(g.DatosInvalidos):
        g.aprobar(admin, r["propuesta_id"])
    prop = [p for p in g.listar_propuestas(admin, "todas") if p["id"] == r["propuesta_id"]][0]
    assert prop["estado"] == "rechazada" and prop["motivo_revision"] == "no es oficial"


def test_aislamiento_entre_empresas(empresa, otra_empresa):
    _, admin_a, agente_a = empresa
    _, admin_b, agente_b = otra_empresa
    p = g.proponer(agente_a, "definicion", {"termino": "Secreto A", "definicion": "solo A"}, "x")
    with pytest.raises(g.DatosInvalidos):  # el admin de B no ve propuestas de A
        g.aprobar(admin_b, p["propuesta_id"])
    g.aprobar(admin_a, p["propuesta_id"])
    assert l.obtener_definicion(agente_b, "Secreto A")["encontrada"] is False
    assert l.buscar_contexto(agente_b, "Secreto A")["cantidad"] == 0


def test_validaciones(empresa):
    _, _, agente = empresa
    with pytest.raises(g.DatosInvalidos):
        g.proponer(agente, "definicion", {"termino": "X"}, "falta la definición")
    with pytest.raises(g.DatosInvalidos):
        g.proponer(agente, "definicion", {"termino": "X", "definicion": "y", "empresa_id": "otra"}, "campo extra")
    with pytest.raises(g.DatosInvalidos):
        g.proponer(agente, "regla", {"nombre": "R", "aplica_a": "x", "restriccion": "y"}, "  ")
    with pytest.raises(g.DatosInvalidos):
        g.proponer(agente, "tabla_inventada", {}, "x")


def test_token_invalido():
    with pytest.raises(g.AccesoDenegado):
        g.identificar("lgs_agt_falso")
    with pytest.raises(g.AccesoDenegado):
        g.identificar(None)


def test_auditoria_registra_e_inmutable(empresa):
    eid, admin, agente = empresa
    p = g.proponer(agente, "definicion", {"termino": "Aud", "definicion": "d"}, "x")
    g.aprobar(admin, p["propuesta_id"])
    with db.conexion() as conn:
        acciones = [r["accion"] for r in conn.execute(
            "select accion from auditoria where empresa_id = %s order by id", (eid,)).fetchall()]
    assert "proponer_actualizacion" in acciones and "aprobar_propuesta" in acciones
    with pytest.raises(psycopg.errors.RaiseException):
        with db.conexion() as conn:
            conn.execute("delete from auditoria where empresa_id = %s", (eid,))


def test_chunking_tamano_y_solapamiento():
    parrafos = [f"Párrafo número {i}. " + "palabra " * 120 for i in range(12)]
    partes = chunking.dividir("\n\n".join(parrafos), tokens=700, solapamiento=100)
    assert len(partes) > 1
    assert all(chunking.contar_tokens(p) <= 700 for p in partes)
    for a, b in zip(partes, partes[1:]):  # cada chunk arranca con el final del anterior
        assert b.split("\n\n")[0] in a


def _como_usuario(conn, eid, rol):
    conn.execute("set local role authenticated")
    conn.execute("select set_config('request.jwt.claims', %s, true)",
                 (json.dumps({"app_metadata": {"empresa_id": eid, "rol": rol}}),))


def test_rls_autenticado_solo_ve_aprobado_de_su_empresa(empresa, otra_empresa):
    eid, admin, agente = empresa
    eid_b, admin_b, agente_b = otra_empresa
    aprobar_nueva(admin, agente, "definicion", {"termino": "Visible", "definicion": "ok"})
    g.proponer(agente, "definicion", {"termino": "Pendiente", "definicion": "no"}, "x")
    aprobar_nueva(admin_b, agente_b, "definicion", {"termino": "De B", "definicion": "no"})
    r = g.cargar_documento(agente, "Doc borrador", "texto en borrador")
    with db.pool().connection() as conn:
        with conn.transaction():
            _como_usuario(conn, eid, "agente")
            terminos = [f["termino"] for f in conn.execute("select termino from definiciones").fetchall()]
            assert terminos == ["Visible"]
            assert conn.execute("select count(*) n from chunks").fetchone()["n"] == 0
            assert conn.execute("select count(*) n from propuestas").fetchone()["n"] == 0
            assert conn.execute("select count(*) n from tokens_acceso").fetchone()["n"] == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("insert into definiciones (empresa_id, termino, definicion, estado) "
                             "values (%s, 'Directo', 'x', 'aprobado')", (eid,))
        with conn.transaction():
            _como_usuario(conn, eid, "admin")
            n = conn.execute("select count(*) n from chunks where documento_id = %s",
                             (r["documento_id"],)).fetchone()["n"]
            assert n == 1
            assert conn.execute("select count(*) n from propuestas").fetchone()["n"] >= 2


def test_rls_anon_no_ve_nada(empresa):
    eid, admin, agente = empresa
    aprobar_nueva(admin, agente, "definicion", {"termino": "Algo", "definicion": "x"})
    with db.pool().connection() as conn:
        with conn.transaction():
            conn.execute("set local role anon")
            for t in ("empresas", "definiciones", "procesos", "reglas", "documentos", "chunks",
                      "propuestas", "auditoria", "tokens_acceso"):
                assert conn.execute(f"select count(*) n from {t}").fetchone()["n"] == 0
