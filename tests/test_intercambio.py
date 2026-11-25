"""Exportar e importar conocimiento entre empresas."""
import json

import pytest

from logos import cli, gobernanza as g, intercambio, lectura as l
from logos.gobernanza import AccesoDenegado, DatosInvalidos


def _cargar_base(admin):
    for tipo, c in [("definicion", {"termino": "MRR", "definicion": "Ingreso mensual recurrente", "categoria": "Finanzas"}),
                    ("proceso", {"nombre": "Cierre de caja", "pasos": ["Contar", "Registrar"], "responsable": "Caja"}),
                    ("regla", {"nombre": "Sin descuentos", "aplica_a": "ventas", "restriccion": "No ofrecer descuentos"})]:
        g.aprobar(admin, g.proponer(admin, tipo, c, "base")["propuesta_id"])
    r = g.cargar_documento(admin, "FAQ envíos", "Los envíos tardan 48 horas.", "FAQ", "web")
    g.aprobar(admin, r["propuesta_id"])


def test_exportar_solo_vigente(empresa):
    _, admin, agente = empresa
    _cargar_base(admin)
    g.proponer(agente, "definicion", {"termino": "Churn", "definicion": "pendiente"}, "x")  # no aprobada
    datos = intercambio.exportar(admin)
    assert datos["formato"] == "logos/1"
    assert [d["termino"] for d in datos["definicion"]] == ["MRR"]
    assert datos["proceso"][0]["pasos"] == ["Contar", "Registrar"]
    assert datos["documento"][0]["contenido"] == "Los envíos tardan 48 horas."
    md = intercambio.a_markdown(datos)
    assert "## Procesos" in md and "1. Contar" in md and "**Aplica a:** ventas" in md
    with pytest.raises(AccesoDenegado):
        intercambio.exportar(agente)


def test_importar_en_otra_empresa_y_es_idempotente(empresa, otra_empresa):
    _, admin, _ = empresa
    _, admin2, agente2 = otra_empresa
    _cargar_base(admin)
    datos = json.loads(json.dumps(intercambio.exportar(admin)))  # ida y vuelta por JSON

    r = intercambio.importar(admin2, datos, "backup.json")
    assert (r["nuevos"], r["actualizados"], r["sin_cambios"]) == (4, 0, 0)
    assert l.obtener_definicion(agente2, "mrr")["encontrada"]
    assert l.buscar_contexto(agente2, "envíos 48 horas", "documentos")["cantidad"] == 1

    r = intercambio.importar(admin2, datos, "backup.json")
    assert (r["nuevos"], r["actualizados"], r["sin_cambios"]) == (0, 0, 4)

    datos["definicion"][0]["definicion"] = "Ingreso mensual recurrente, sin IVA"
    r = intercambio.importar(admin2, datos, "backup.json")
    assert (r["nuevos"], r["actualizados"], r["sin_cambios"]) == (0, 1, 3)
    d = l.obtener_definicion(agente2, "MRR")["definicion"]
    assert d["version"] == 2 and d["definicion"].endswith("sin IVA")


def test_importar_pendiente_no_rige(empresa):
    _, admin, agente = empresa
    r = intercambio.importar(admin, {"definicion": [{"termino": "NPS", "definicion": "Net promoter score"}]},
                             aprobar=False)
    assert r["estado"] == "pendiente de aprobación" and len(r["propuestas"]) == 1
    assert not l.obtener_definicion(agente, "NPS")["encontrada"]
    assert len(g.listar_propuestas(admin, "pendiente")) == 1


def test_importar_valida_todo_antes_de_escribir(empresa):
    _, admin, agente = empresa
    malo = {"definicion": [{"termino": "Bien", "definicion": "ok"}, {"termino": "Mal"}]}
    with pytest.raises(DatosInvalidos):
        intercambio.importar(admin, malo)
    assert not l.obtener_definicion(agente, "Bien")["encontrada"]
    with pytest.raises(DatosInvalidos):
        intercambio.importar(admin, {"formato": "otro/9", "definicion": []})
    with pytest.raises(AccesoDenegado):
        intercambio.importar(agente, {"definicion": [{"termino": "X", "definicion": "y"}]})


def test_cli_exportar_importar(empresa, otra_empresa, tmp_path, capsys):
    eid, admin, _ = empresa
    eid2, _, agente2 = otra_empresa
    _cargar_base(admin)
    t1, t2 = g.crear_token(eid, "Admin cli", "admin"), g.crear_token(eid2, "Admin cli", "admin")
    archivo, md = tmp_path / "logos.json", tmp_path / "logos.md"
    cli.main(["exportar", "--token", t1, "--salida", str(archivo)])
    cli.main(["exportar", "--token", t1, "--formato", "markdown", "--salida", str(md)])
    cli.main(["importar", str(archivo), "--token", t2])
    assert "Nuevos: 4" in capsys.readouterr().out
    assert md.read_text().startswith("# Conocimiento de")
    assert l.obtener_reglas(agente2, "ventas")["cantidad"] == 1
