"""Identidad, auditoría, propuestas, aprobación y versionado.

Reglas que este módulo garantiza:
- La empresa sale SIEMPRE del token, nunca de lo que mande el agente.
- Un agente solo puede crear propuestas (y documentos en borrador).
- Solo un admin aprueba o rechaza; al aprobar se versiona y lo anterior queda obsoleto.
- Todo queda en `auditoria`.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from . import chunking, db, embeddings


class AccesoDenegado(PermissionError):
    pass


class DatosInvalidos(ValueError):
    pass


@dataclass(frozen=True)
class Identidad:
    empresa_id: str
    token_id: str
    nombre: str
    rol: str  # "agente" | "admin"

    @property
    def es_admin(self) -> bool:
        return self.rol == "admin"


# ---------------------------------------------------------------- tokens

def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def crear_token(empresa_id: str, nombre: str, rol: str) -> str:
    """Crea un token y devuelve el valor en claro (se muestra una sola vez)."""
    if rol not in ("agente", "admin"):
        raise DatosInvalidos("rol debe ser 'agente' o 'admin'")
    prefijo = "lgs_adm_" if rol == "admin" else "lgs_agt_"
    token = prefijo + secrets.token_urlsafe(32)
    with db.conexion() as conn:
        conn.execute(
            "insert into tokens_acceso (empresa_id, nombre, rol, token_hash) values (%s, %s, %s, %s)",
            (empresa_id, nombre, rol, hash_token(token)),
        )
        _auditar(conn, None, "crear_token", {"nombre": nombre, "rol": rol}, empresa_id=empresa_id,
                 actor="sistema")
    return token


def identificar(token: str | None) -> Identidad:
    if not token:
        raise AccesoDenegado("Falta el token de acceso a Logos.")
    with db.conexion() as conn:
        fila = conn.execute(
            """update tokens_acceso set ultimo_uso = now()
               where token_hash = %s and activo
               returning id, empresa_id, nombre, rol""",
            (hash_token(token),),
        ).fetchone()
    if not fila:
        raise AccesoDenegado("Token inválido o desactivado.")
    return Identidad(str(fila["empresa_id"]), str(fila["id"]), fila["nombre"], fila["rol"])


def exigir_admin(ident: Identidad) -> None:
    if not ident.es_admin:
        raise AccesoDenegado(
            "Esta acción es solo para administradores. Como agente podés usar "
            "proponer_actualizacion y un humano la revisa."
        )


# ---------------------------------------------------------------- auditoría

def _auditar(conn: Connection, ident: Identidad | None, accion: str, detalle: dict[str, Any],
             empresa_id: str | None = None, actor: str | None = None) -> None:
    conn.execute(
        "insert into auditoria (empresa_id, actor, rol, accion, detalle) values (%s, %s, %s, %s, %s)",
        (
            empresa_id or (ident.empresa_id if ident else None),
            actor or (ident.nombre if ident else "desconocido"),
            ident.rol if ident else None,
            accion,
            Jsonb(_serializable(detalle)),
        ),
    )


def auditar(ident: Identidad | None, accion: str, detalle: dict[str, Any], **kw: Any) -> None:
    with db.conexion() as conn:
        _auditar(conn, ident, accion, detalle, **kw)


def _serializable(valor: Any) -> Any:
    return json.loads(json.dumps(valor, default=str))


# ---------------------------------------------------------------- propuestas

ESQUEMAS: dict[str, dict[str, Any]] = {
    "definicion": {"clave": "termino", "requeridos": ["termino", "definicion"],
                   "opcionales": ["categoria", "dueno"]},
    "proceso": {"clave": "nombre", "requeridos": ["nombre"],
                "opcionales": ["descripcion", "pasos", "responsable", "restricciones"]},
    "regla": {"clave": "nombre", "requeridos": ["nombre", "aplica_a", "restriccion"],
              "opcionales": ["descripcion"]},
    "documento": {"clave": "titulo", "requeridos": [], "opcionales": ["documento_id", "titulo"]},
}
TABLAS = {"definicion": "definiciones", "proceso": "procesos", "regla": "reglas", "documento": "documentos"}


def validar_contenido(tipo: str, contenido: dict[str, Any]) -> dict[str, Any]:
    if tipo not in ESQUEMAS:
        raise DatosInvalidos(f"tipo debe ser uno de: {', '.join(ESQUEMAS)}")
    if not isinstance(contenido, dict):
        raise DatosInvalidos("contenido debe ser un objeto")
    esquema = ESQUEMAS[tipo]
    contenido = dict(contenido)
    if "dueño" in contenido:  # aceptamos la ñ
        contenido["dueno"] = contenido.pop("dueño")
    accion = contenido.pop("accion", "actualizar")
    if accion not in ("actualizar", "obsoletar"):
        raise DatosInvalidos("accion debe ser 'actualizar' u 'obsoletar'")
    permitidos = set(esquema["requeridos"]) | set(esquema["opcionales"])
    sobrantes = set(contenido) - permitidos
    if sobrantes:
        raise DatosInvalidos(f"Campos no reconocidos para {tipo}: {', '.join(sorted(sobrantes))}. "
                             f"Permitidos: {', '.join(sorted(permitidos))}")
    if tipo == "documento":
        if not (contenido.get("documento_id") or contenido.get("titulo")):
            raise DatosInvalidos("Para documentos indicá documento_id (o titulo para obsoletar). "
                                 "Para cargar uno nuevo usá cargar_documento.")
        if accion == "actualizar" and not contenido.get("documento_id"):
            raise DatosInvalidos("Para aprobar un documento hace falta su documento_id "
                                 "(lo devuelve cargar_documento).")
    else:
        # La clave (termino/nombre) siempre. Una definición nueva o cambiada necesita su texto.
        # En procesos y reglas se puede proponer solo lo que cambia: el resto se hereda de la
        # versión vigente al aprobar (y si no hay vigente, se exige todo en ese momento).
        clave = esquema["clave"]
        necesarios = [clave]
        if accion == "actualizar" and tipo == "definicion":
            necesarios = esquema["requeridos"]
        faltan = [c for c in necesarios if not str(contenido.get(c) or "").strip()]
        if faltan:
            raise DatosInvalidos(f"Faltan campos para {tipo}: {', '.join(faltan)}")
    if "pasos" in contenido and not isinstance(contenido["pasos"], list):
        raise DatosInvalidos("pasos debe ser una lista (de textos u objetos)")
    for k, v in contenido.items():
        if isinstance(v, str):
            contenido[k] = v.strip()
    contenido["accion"] = accion
    return contenido


def proponer(ident: Identidad, tipo: str, contenido: dict[str, Any], justificacion: str) -> dict[str, Any]:
    contenido = validar_contenido(tipo, contenido)
    if not (justificacion or "").strip():
        raise DatosInvalidos("La justificación es obligatoria: explicá por qué hace falta el cambio.")
    with db.conexion() as conn:
        if tipo == "documento" and contenido.get("documento_id"):
            _doc_de_empresa(conn, ident, contenido["documento_id"])
        fila = conn.execute(
            """insert into propuestas (empresa_id, tipo, contenido, justificacion, propuesto_por)
               values (%s, %s, %s, %s, %s) returning id, creado_en""",
            (ident.empresa_id, tipo, Jsonb(contenido), justificacion.strip(), ident.nombre),
        ).fetchone()
        _auditar(conn, ident, "proponer_actualizacion",
                 {"propuesta_id": fila["id"], "tipo": tipo, "accion": contenido["accion"],
                  "titulo": contenido.get(ESQUEMAS[tipo]["clave"]) or contenido.get("titulo")})
    return {"propuesta_id": str(fila["id"]), "estado": "pendiente",
            "mensaje": "Propuesta registrada. Un administrador la tiene que aprobar antes de que cuente."}


def listar_propuestas(ident: Identidad, estado: str = "pendiente", limite: int = 50) -> list[dict[str, Any]]:
    exigir_admin(ident)
    with db.conexion() as conn:
        filas = conn.execute(
            """select id, tipo, contenido, justificacion, propuesto_por, estado, revisado_por,
                      motivo_revision, creado_en, revisado_en
               from propuestas where empresa_id = %s and (%s = 'todas' or estado = %s)
               order by creado_en desc limit %s""",
            (ident.empresa_id, estado, estado, limite),
        ).fetchall()
    return [_serializable(f) for f in filas]


def _bloquear_propuesta(conn: Connection, ident: Identidad, propuesta_id: str) -> dict[str, Any]:
    fila = conn.execute(
        "select * from propuestas where id = %s and empresa_id = %s for update",
        (propuesta_id, ident.empresa_id),
    ).fetchone()
    if not fila:
        raise DatosInvalidos("No existe esa propuesta en tu empresa.")
    if fila["estado"] != "pendiente":
        raise DatosInvalidos(f"La propuesta ya está {fila['estado']}.")
    return fila


def _titulo(prop: dict[str, Any]) -> str | None:
    c = prop["contenido"]
    return c.get(ESQUEMAS[prop["tipo"]]["clave"]) or c.get("titulo")


def aprobar(ident: Identidad, propuesta_id: str) -> dict[str, Any]:
    exigir_admin(ident)
    with db.conexion() as conn:
        prop = _bloquear_propuesta(conn, ident, propuesta_id)
        resultado = _aplicar(conn, ident, prop)
        conn.execute(
            """update propuestas set estado = 'aprobada', revisado_por = %s, revisado_en = now(),
               resultado_id = %s where id = %s""",
            (ident.nombre, resultado.get("id"), propuesta_id),
        )
        _auditar(conn, ident, "aprobar_propuesta", {"propuesta_id": propuesta_id, "tipo": prop["tipo"],
                                                     "titulo": _titulo(prop), **resultado})
    return {"propuesta_id": propuesta_id, "estado": "aprobada", **_serializable(resultado)}


def rechazar(ident: Identidad, propuesta_id: str, motivo: str) -> dict[str, Any]:
    exigir_admin(ident)
    if not (motivo or "").strip():
        raise DatosInvalidos("Indicá el motivo del rechazo.")
    with db.conexion() as conn:
        prop = _bloquear_propuesta(conn, ident, propuesta_id)
        if prop["tipo"] == "documento" and prop["contenido"].get("documento_id"):
            conn.execute(
                """update documentos set estado = 'obsoleto', actualizado_en = now(),
                   metadata = metadata || '{"rechazado": true}'::jsonb
                   where id = %s and empresa_id = %s and estado = 'borrador'""",
                (prop["contenido"]["documento_id"], ident.empresa_id),
            )
        conn.execute(
            """update propuestas set estado = 'rechazada', revisado_por = %s, revisado_en = now(),
               motivo_revision = %s where id = %s""",
            (ident.nombre, motivo.strip(), propuesta_id),
        )
        _auditar(conn, ident, "rechazar_propuesta", {"propuesta_id": propuesta_id, "motivo": motivo,
                                                      "tipo": prop["tipo"], "titulo": _titulo(prop)})
    return {"propuesta_id": propuesta_id, "estado": "rechazada"}


# ---------------------------------------------------------------- aplicar (versionado)

def _bloqueo_clave(conn: Connection, empresa_id: str, tabla: str, clave: str) -> None:
    conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"{empresa_id}|{tabla}|{clave.lower()}",))


def _aplicar(conn: Connection, ident: Identidad, prop: dict[str, Any]) -> dict[str, Any]:
    tipo, c = prop["tipo"], dict(prop["contenido"])
    accion = c.pop("accion", "actualizar")
    if tipo == "documento":
        return _aplicar_documento(conn, ident, c, accion)

    tabla, clave = TABLAS[tipo], ESQUEMAS[tipo]["clave"]
    valor_clave = c[clave]
    _bloqueo_clave(conn, ident.empresa_id, tabla, valor_clave)
    vigente = conn.execute(
        f"select * from {tabla} where empresa_id = %s and lower({clave}) = lower(%s) and estado = 'aprobado'",
        (ident.empresa_id, valor_clave),
    ).fetchone()

    if accion == "obsoletar":
        if not vigente:
            raise DatosInvalidos(f"No hay {tipo} aprobado/a con {clave} '{valor_clave}' para dar de baja.")
        conn.execute(f"update {tabla} set estado = 'obsoleto', actualizado_en = now() where id = %s",
                     (vigente["id"],))
        return {"id": vigente["id"], "accion": "obsoletado", "version": vigente["version"]}

    campos = ESQUEMAS[tipo]["requeridos"] + ESQUEMAS[tipo]["opcionales"]
    datos = {k: (vigente[k] if vigente else None) for k in campos}
    datos.update({k: v for k, v in c.items() if k in campos})
    if vigente:  # conservar la forma exacta (mayúsculas) del término/nombre vigente
        datos[clave] = vigente[clave]
    faltan = [k for k in ESQUEMAS[tipo]["requeridos"] if not datos.get(k)]
    if faltan:
        raise DatosInvalidos(f"No se puede aprobar: faltan {', '.join(faltan)} y no hay versión anterior.")
    if tipo == "proceso":
        datos["pasos"] = Jsonb(datos.get("pasos") or [])

    maxv = conn.execute(
        f"select coalesce(max(version), 0) v from {tabla} where empresa_id = %s and lower({clave}) = lower(%s)",
        (ident.empresa_id, valor_clave),
    ).fetchone()["v"]
    if vigente:
        conn.execute(f"update {tabla} set estado = 'obsoleto', actualizado_en = now() where id = %s",
                     (vigente["id"],))
    columnas = list(datos) + ["empresa_id", "version", "estado", "propuesta_id"]
    valores = list(datos.values()) + [ident.empresa_id, maxv + 1, "aprobado", prop["id"]]
    nueva = conn.execute(
        f"insert into {tabla} ({', '.join(columnas)}) values ({', '.join(['%s'] * len(valores))}) "
        f"returning id, version",
        valores,
    ).fetchone()
    return {"id": nueva["id"], "accion": "nueva_version" if vigente else "creado",
            "version": nueva["version"], "reemplaza": vigente["id"] if vigente else None}


def _doc_de_empresa(conn: Connection, ident: Identidad, documento_id: str) -> dict[str, Any]:
    doc = conn.execute("select * from documentos where id = %s and empresa_id = %s",
                       (documento_id, ident.empresa_id)).fetchone()
    if not doc:
        raise DatosInvalidos("No existe ese documento en tu empresa.")
    return doc


def _aplicar_documento(conn: Connection, ident: Identidad, c: dict[str, Any], accion: str) -> dict[str, Any]:
    if accion == "obsoletar":
        if c.get("documento_id"):
            doc = _doc_de_empresa(conn, ident, c["documento_id"])
        else:
            doc = conn.execute(
                "select * from documentos where empresa_id = %s and lower(titulo) = lower(%s) and estado = 'aprobado'",
                (ident.empresa_id, c["titulo"]),
            ).fetchone()
        if not doc or doc["estado"] != "aprobado":
            raise DatosInvalidos("No hay un documento aprobado que coincida para dar de baja.")
        conn.execute("update documentos set estado = 'obsoleto', actualizado_en = now() where id = %s",
                     (doc["id"],))
        return {"id": doc["id"], "accion": "obsoletado", "version": doc["version"]}

    doc = _doc_de_empresa(conn, ident, c["documento_id"])
    if doc["estado"] != "borrador":
        raise DatosInvalidos(f"El documento está {doc['estado']}; solo se aprueban borradores.")
    _bloqueo_clave(conn, ident.empresa_id, "documentos", doc["titulo"])
    vigente = conn.execute(
        "select id, version from documentos where empresa_id = %s and lower(titulo) = lower(%s) and estado = 'aprobado'",
        (ident.empresa_id, doc["titulo"]),
    ).fetchone()
    maxv = conn.execute(
        """select coalesce(max(version), 0) v from documentos
           where empresa_id = %s and lower(titulo) = lower(%s) and estado <> 'borrador'""",
        (ident.empresa_id, doc["titulo"]),
    ).fetchone()["v"]
    if vigente:
        conn.execute("update documentos set estado = 'obsoleto', actualizado_en = now() where id = %s",
                     (vigente["id"],))
    conn.execute(
        "update documentos set estado = 'aprobado', version = %s, actualizado_en = now() where id = %s",
        (maxv + 1, doc["id"]),
    )
    return {"id": doc["id"], "accion": "nueva_version" if vigente else "creado", "version": maxv + 1,
            "reemplaza": vigente["id"] if vigente else None}


# ---------------------------------------------------------------- ingesta

def cargar_documento(ident: Identidad, titulo: str, contenido: str, tipo: str | None = None,
                     fuente: str | None = None, metadata: dict[str, Any] | None = None,
                     justificacion: str | None = None) -> dict[str, Any]:
    """Limpia, divide, genera embeddings y guarda en borrador + crea la propuesta de aprobación."""
    titulo = (titulo or "").strip()
    texto = chunking.limpiar(contenido or "")
    if not titulo or not texto:
        raise DatosInvalidos("El documento necesita título y contenido.")
    partes = chunking.dividir(texto)

    aviso = None
    vectores: list[list[float] | None]
    try:
        vectores = list(embeddings.generar(partes))
    except embeddings.EmbeddingsNoDisponibles as e:
        vectores = [None] * len(partes)
        aviso = f"Se guardó sin embeddings ({e}). La búsqueda por texto funciona; correr reindexar después."

    meta = dict(metadata or {})
    meta.update({"tokens": chunking.contar_tokens(texto), "embeddings": embeddings.proveedor()
                 if aviso is None else None})
    with db.conexion() as conn:
        doc = conn.execute(
            """insert into documentos (empresa_id, titulo, contenido, tipo, fuente, estado, metadata)
               values (%s, %s, %s, %s, %s, 'borrador', %s) returning id""",
            (ident.empresa_id, titulo, texto, tipo, fuente, Jsonb(meta)),
        ).fetchone()
        with conn.cursor() as cur:
            cur.executemany(
                """insert into chunks (empresa_id, documento_id, orden, contenido, embedding, metadata)
                   values (%s, %s, %s, %s, %s::extensions.vector, %s)""",
                [
                    (ident.empresa_id, doc["id"], i, parte,
                     embeddings.a_pgvector(v) if v is not None else None,
                     Jsonb({"tokens": chunking.contar_tokens(parte)}))
                    for i, (parte, v) in enumerate(zip(partes, vectores))
                ],
            )
        prop = conn.execute(
            """insert into propuestas (empresa_id, tipo, contenido, justificacion, propuesto_por)
               values (%s, 'documento', %s, %s, %s) returning id""",
            (ident.empresa_id,
             Jsonb({"accion": "actualizar", "documento_id": str(doc["id"]), "titulo": titulo,
                    "tipo": tipo, "fuente": fuente, "chunks": len(partes)}),
             (justificacion or "").strip() or f"Carga del documento '{titulo}'",
             ident.nombre),
        ).fetchone()
        _auditar(conn, ident, "cargar_documento",
                 {"documento_id": doc["id"], "propuesta_id": prop["id"], "titulo": titulo,
                  "chunks": len(partes), "sin_embeddings": aviso is not None})
    salida = {"documento_id": str(doc["id"]), "propuesta_id": str(prop["id"]), "estado": "borrador",
              "chunks": len(partes),
              "mensaje": "Documento guardado en borrador. No se usa hasta que un admin apruebe la propuesta."}
    if aviso:
        salida["aviso"] = aviso
    return salida


def reindexar(empresa_id: str | None = None) -> int:
    """Genera embeddings de los chunks que no los tienen. Devuelve cuántos actualizó."""
    with db.conexion() as conn:
        filas = conn.execute(
            "select id, contenido from chunks where embedding is null and (%s::uuid is null or empresa_id = %s)",
            (empresa_id, empresa_id),
        ).fetchall()
    if not filas:
        return 0
    vectores = embeddings.generar([f["contenido"] for f in filas])
    with db.conexion() as conn:
        with conn.cursor() as cur:
            cur.executemany("update chunks set embedding = %s::extensions.vector where id = %s",
                            [(embeddings.a_pgvector(v), f["id"]) for f, v in zip(filas, vectores)])
    return len(filas)
