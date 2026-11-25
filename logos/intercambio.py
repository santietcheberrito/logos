"""Exportar e importar el conocimiento de una empresa.

El export trae solo lo vigente (aprobado), en JSON (para respaldo y migraciones) o Markdown (para
leerlo). El JSON usa el mismo formato que `ejemplos/conocimiento.json`, más los documentos.

El import pasa por la gobernanza normal: cada elemento nuevo o cambiado entra como propuesta (y
se aprueba, salvo que se pida dejarlo pendiente). Lo que ya está igual se saltea, así que importar
dos veces el mismo archivo no crea versiones nuevas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import chunking, db, gobernanza
from .gobernanza import DatosInvalidos, Identidad, _serializable

FORMATO = "logos/1"

_CAMPOS = {
    "definicion": ("definiciones", "termino", ("definicion", "categoria", "dueno")),
    "proceso": ("procesos", "nombre", ("descripcion", "pasos", "responsable", "restricciones")),
    "regla": ("reglas", "nombre", ("descripcion", "aplica_a", "restriccion")),
}


def _vigentes(conn, empresa_id: str, tipo: str) -> list[dict[str, Any]]:
    tabla, clave, campos = _CAMPOS[tipo]
    filas = conn.execute(
        f"""select {clave}, {', '.join(campos)}, version from {tabla}
            where empresa_id = %s and estado = 'aprobado' order by lower({clave})""",
        (empresa_id,),
    ).fetchall()
    return [{k: v for k, v in _serializable(f).items() if v not in (None, "")} for f in filas]


def exportar(ident: Identidad) -> dict[str, Any]:
    gobernanza.exigir_admin(ident)
    with db.conexion() as conn:
        empresa = conn.execute("select nombre from empresas where id = %s", (ident.empresa_id,)).fetchone()
        datos: dict[str, Any] = {
            "formato": FORMATO,
            "empresa": empresa["nombre"] if empresa else None,
            "exportado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        for tipo in _CAMPOS:
            datos[tipo] = _vigentes(conn, ident.empresa_id, tipo)
        docs = conn.execute(
            """select titulo, tipo, fuente, contenido, version from documentos
               where empresa_id = %s and estado = 'aprobado' order by lower(titulo)""",
            (ident.empresa_id,),
        ).fetchall()
        datos["documento"] = [{k: v for k, v in dict(d).items() if v not in (None, "")} for d in docs]
        gobernanza._auditar(conn, ident, "exportar", {"cantidades": _cantidades(datos)})
    return datos


def _cantidades(datos: dict[str, Any]) -> dict[str, int]:
    return {t: len(datos.get(t) or []) for t in (*_CAMPOS, "documento")}


def a_markdown(datos: dict[str, Any]) -> str:
    """Versión legible del export (no se puede volver a importar: para eso está el JSON)."""
    salida = [f"# Conocimiento de {datos.get('empresa') or 'la empresa'}",
              f"Exportado el {datos.get('exportado_en', '')}. Solo contenido aprobado y vigente.", ""]
    if datos.get("definicion"):
        salida.append("## Definiciones\n")
        for d in datos["definicion"]:
            extra = " · ".join(x for x in (d.get("categoria"), d.get("dueno") and f"Dueño: {d['dueno']}") if x)
            salida.append(f"### {d['termino']} (v{d.get('version', 1)})\n\n{d['definicion']}\n")
            if extra:
                salida.append(f"_{extra}_\n")
    if datos.get("proceso"):
        salida.append("## Procesos\n")
        for p in datos["proceso"]:
            salida.append(f"### {p['nombre']} (v{p.get('version', 1)})\n")
            if p.get("descripcion"):
                salida.append(f"{p['descripcion']}\n")
            for i, paso in enumerate(p.get("pasos") or [], 1):
                salida.append(f"{i}. {paso if isinstance(paso, str) else paso.get('paso') or paso}")
            if p.get("pasos"):
                salida.append("")
            if p.get("responsable"):
                salida.append(f"**Responsable:** {p['responsable']}\n")
            if p.get("restricciones"):
                salida.append(f"**Restricciones:** {p['restricciones']}\n")
    if datos.get("regla"):
        salida.append("## Reglas\n")
        for r in datos["regla"]:
            salida.append(f"### {r['nombre']} (v{r.get('version', 1)})\n\n**Aplica a:** {r['aplica_a']}\n\n"
                          f"{r['restriccion']}\n")
            if r.get("descripcion"):
                salida.append(f"{r['descripcion']}\n")
    if datos.get("documento"):
        salida.append("## Documentos\n")
        for d in datos["documento"]:
            meta = " · ".join(x for x in (d.get("tipo"), d.get("fuente") and f"Fuente: {d['fuente']}") if x)
            salida.append(f"### {d['titulo']} (v{d.get('version', 1)})\n")
            if meta:
                salida.append(f"_{meta}_\n")
            salida.append(f"{d['contenido']}\n")
    return "\n".join(salida).rstrip() + "\n"


def _normalizar(valor: Any) -> Any:
    if isinstance(valor, str):
        return valor.strip() or None
    return valor or None


def importar(ident: Identidad, datos: dict[str, Any], origen: str = "archivo",
             aprobar: bool = True) -> dict[str, Any]:
    """Carga un export de Logos (o un archivo con el mismo formato) en la empresa del token."""
    gobernanza.exigir_admin(ident)
    if not isinstance(datos, dict):
        raise DatosInvalidos("El archivo tiene que ser un objeto JSON con definicion, proceso, regla y/o documento.")
    if datos.get("formato") not in (None, FORMATO):
        raise DatosInvalidos(f"Formato no reconocido: {datos.get('formato')} (se espera {FORMATO}).")
    listas = {t: datos.get(t) or [] for t in (*_CAMPOS, "documento")}
    if not any(listas.values()):
        raise DatosInvalidos("El archivo no trae definiciones, procesos, reglas ni documentos.")
    for tipo, items in listas.items():
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise DatosInvalidos(f"'{tipo}' tiene que ser una lista de objetos.")

    justificacion = f"Importación desde {origen}"
    resumen = {"nuevos": 0, "actualizados": 0, "sin_cambios": 0, "propuestas": []}

    with db.conexion() as conn:
        actuales = {t: {x[_CAMPOS[t][1]].lower(): x for x in _vigentes(conn, ident.empresa_id, t)} for t in _CAMPOS}
        docs = {d["titulo"].lower(): d["contenido"] for d in conn.execute(
            "select titulo, contenido from documentos where empresa_id = %s and estado = 'aprobado'",
            (ident.empresa_id,)).fetchall()}

    # Validar todo antes de escribir nada: un archivo con un error no deja la importación por la mitad.
    pendientes: list[tuple[str, dict[str, Any], bool]] = []
    for tipo in _CAMPOS:
        _, clave, campos = _CAMPOS[tipo]
        for item in listas[tipo]:
            contenido = {k: v for k, v in item.items() if k != "version"}
            contenido = gobernanza.validar_contenido(tipo, contenido)
            contenido.pop("accion")
            previo = actuales[tipo].get(str(contenido[clave]).lower())
            if previo and all(_normalizar(previo.get(c)) == _normalizar(contenido.get(c)) for c in campos):
                resumen["sin_cambios"] += 1
                continue
            pendientes.append((tipo, contenido, previo is not None))
    documentos: list[tuple[dict[str, Any], bool]] = []
    for d in listas["documento"]:
        titulo, texto = str(d.get("titulo") or "").strip(), str(d.get("contenido") or "")
        if not titulo or not texto.strip():
            raise DatosInvalidos("Cada documento necesita titulo y contenido.")
        previo = docs.get(titulo.lower())
        if previo is not None and previo == chunking.limpiar(texto):
            resumen["sin_cambios"] += 1
            continue
        documentos.append((d, previo is not None))

    for tipo, contenido, existe in pendientes:
        p = gobernanza.proponer(ident, tipo, contenido, justificacion)
        if aprobar:
            gobernanza.aprobar(ident, p["propuesta_id"])
        resumen["actualizados" if existe else "nuevos"] += 1
        resumen["propuestas"].append(p["propuesta_id"])
    for d, existe in documentos:
        r = gobernanza.cargar_documento(ident, d["titulo"], d["contenido"], d.get("tipo"), d.get("fuente"),
                                        justificacion=justificacion)
        if aprobar:
            gobernanza.aprobar(ident, r["propuesta_id"])
        resumen["actualizados" if existe else "nuevos"] += 1
        resumen["propuestas"].append(r["propuesta_id"])

    resumen["estado"] = "aprobado" if aprobar else "pendiente de aprobación"
    gobernanza.auditar(ident, "importar", {"origen": origen, "aprobado": aprobar,
                                           **{k: resumen[k] for k in ("nuevos", "actualizados", "sin_cambios")}})
    return resumen
