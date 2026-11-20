"""Lectura del conocimiento oficial. Todo filtra por empresa y estado = 'aprobado'."""
from __future__ import annotations

from typing import Any

from . import db, embeddings
from .gobernanza import DatosInvalidos, Identidad, _serializable

SIN_RESULTADO = ("No hay información oficial sobre esto en Logos. "
                 "No improvises: decilo así y, si corresponde, usá proponer_actualizacion.")

TIPOS_BUSQUEDA = ("general", "definiciones", "procesos", "reglas", "documentos")

# Consulta de texto en modo OR (las preguntas naturales no tienen todas las palabras)
_TSQ = "(select nullif(replace(plainto_tsquery('spanish', %(q)s)::text, '&', '|'), '')::tsquery as tsq) q"


def obtener_definicion(ident: Identidad, termino: str) -> dict[str, Any]:
    termino = (termino or "").strip()
    if not termino:
        raise DatosInvalidos("Indicá el término.")
    with db.conexion() as conn:
        fila = conn.execute(
            """select id, termino, definicion, categoria, dueno, version, actualizado_en
               from definiciones where empresa_id = %s and estado = 'aprobado'
               and lower(termino) = lower(%s)""",
            (ident.empresa_id, termino),
        ).fetchone()
        if fila:
            return {"encontrada": True, "definicion": _serializable(fila)}
        sugerencias = conn.execute(
            """select termino from definiciones where empresa_id = %s and estado = 'aprobado'
               and (similarity(lower(termino), lower(%s)) > 0.3 or termino ilike '%%' || %s || '%%')
               order by similarity(lower(termino), lower(%s)) desc limit 5""",
            (ident.empresa_id, termino, termino, termino),
        ).fetchall()
    return {"encontrada": False, "sugerencias": [s["termino"] for s in sugerencias],
            "aviso": SIN_RESULTADO}


def obtener_proceso(ident: Identidad, nombre: str) -> dict[str, Any]:
    nombre = (nombre or "").strip()
    if not nombre:
        raise DatosInvalidos("Indicá el nombre del proceso.")
    with db.conexion() as conn:
        fila = conn.execute(
            """select id, nombre, descripcion, pasos, responsable, restricciones, version, actualizado_en
               from procesos where empresa_id = %s and estado = 'aprobado' and lower(nombre) = lower(%s)""",
            (ident.empresa_id, nombre),
        ).fetchone()
        if not fila:  # nombre parecido: si hay uno solo claro, devolverlo
            candidatos = conn.execute(
                """select id, nombre, descripcion, pasos, responsable, restricciones, version, actualizado_en,
                          similarity(lower(nombre), lower(%s)) sim
                   from procesos where empresa_id = %s and estado = 'aprobado'
                   and (similarity(lower(nombre), lower(%s)) > 0.3 or nombre ilike '%%' || %s || '%%')
                   order by sim desc limit 5""",
                (nombre, ident.empresa_id, nombre, nombre),
            ).fetchall()
            if len(candidatos) == 1 or (candidatos and candidatos[0]["sim"] >= 0.6):
                fila = candidatos[0]
                fila.pop("sim", None)
            else:
                return {"encontrado": False, "sugerencias": [c["nombre"] for c in candidatos],
                        "aviso": SIN_RESULTADO}
    return {"encontrado": True, "proceso": _serializable(fila)}


def obtener_reglas(ident: Identidad, aplica_a: str) -> dict[str, Any]:
    aplica_a = (aplica_a or "").strip().lower()
    with db.conexion() as conn:
        filas = conn.execute(
            """select id, nombre, descripcion, aplica_a, restriccion, version
               from reglas where empresa_id = %s and estado = 'aprobado'
               and (%s = '' or %s = any(string_to_array(regexp_replace(lower(aplica_a), '\\s*,\\s*', ',', 'g'), ','))
                    or lower(aplica_a) in ('todos', 'todas', 'general', '*'))
               order by nombre""",
            (ident.empresa_id, aplica_a, aplica_a),
        ).fetchall()
    salida: dict[str, Any] = {"reglas": [_serializable(f) for f in filas], "cantidad": len(filas)}
    if filas:
        salida["instruccion"] = "Estas reglas son obligatorias. Si una acción las viola, no la hagas."
    else:
        salida["aviso"] = f"No hay reglas aprobadas para '{aplica_a}' (ni generales)."
    return salida


def buscar_contexto(ident: Identidad, query: str, tipo: str = "general", limite: int = 5) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise DatosInvalidos("La consulta está vacía.")
    if tipo not in TIPOS_BUSQUEDA:
        raise DatosInvalidos(f"tipo debe ser uno de: {', '.join(TIPOS_BUSQUEDA)}")
    limite = max(1, min(int(limite or 5), 20))
    p = {"e": ident.empresa_id, "q": query, "k": limite}

    oficiales: list[dict[str, Any]] = []
    fragmentos: list[dict[str, Any]] = []
    modo = "texto"
    with db.conexion() as conn:
        if tipo in ("general", "definiciones"):
            oficiales += _buscar_tabla(conn, p, "definicion", """
                select id, termino as titulo, definicion as contenido, version, categoria as extra,
                       similarity(lower(termino), lower(%(q)s)) sim,
                       ts_rank(to_tsvector('spanish', termino || ' ' || definicion), tsq) rank
                from definiciones, """ + _TSQ + """
                where empresa_id = %(e)s and estado = 'aprobado'""")
        if tipo in ("general", "procesos"):
            oficiales += _buscar_tabla(conn, p, "proceso", """
                select id, nombre as titulo,
                       concat_ws(E'\\n', descripcion, 'Pasos: ' || pasos::text,
                                 'Restricciones: ' || restricciones) as contenido,
                       version, responsable as extra,
                       similarity(lower(nombre), lower(%(q)s)) sim,
                       ts_rank(to_tsvector('spanish', concat_ws(' ', nombre, descripcion, pasos::text, restricciones)), tsq) rank
                from procesos, """ + _TSQ + """
                where empresa_id = %(e)s and estado = 'aprobado'""")
        if tipo in ("general", "reglas"):
            oficiales += _buscar_tabla(conn, p, "regla", """
                select id, nombre as titulo, concat_ws(E'\\n', restriccion, descripcion) as contenido,
                       version, aplica_a as extra,
                       similarity(lower(nombre), lower(%(q)s)) sim,
                       ts_rank(to_tsvector('spanish', concat_ws(' ', nombre, descripcion, restriccion, aplica_a)), tsq) rank
                from reglas, """ + _TSQ + """
                where empresa_id = %(e)s and estado = 'aprobado'""")
        if tipo in ("general", "documentos"):
            fragmentos, modo = _buscar_chunks(conn, p)

    # Un solo ranking; lo estructurado (definiciones/procesos/reglas) lleva un plus porque es
    # conocimiento exacto, pero una coincidencia floja no tapa a un documento muy pertinente.
    resultados = sorted(oficiales + fragmentos,
                        key=lambda r: r["relevancia"] + (0.2 if r["tipo"] != "documento" else 0),
                        reverse=True)[:limite]
    salida: dict[str, Any] = {"consulta": query, "tipo": tipo, "modo_busqueda": modo,
                              "resultados": resultados, "cantidad": len(resultados)}
    if not resultados:
        salida["aviso"] = SIN_RESULTADO
    else:
        salida["instruccion"] = ("Respondé solo con esta información. Si no alcanza para contestar, "
                                 "decí que no está en Logos en vez de completar con suposiciones.")
    return salida


def _buscar_tabla(conn, p: dict[str, Any], tipo: str, base_sql: str) -> list[dict[str, Any]]:
    filas = conn.execute(
        f"""select id, titulo, contenido, version, extra, sim, coalesce(rank, 0) rank
            from ({base_sql}) t
            where sim > 0.3 or coalesce(rank, 0) > 0
            order by greatest(sim, least(coalesce(t.rank, 0) * 10, 1)) desc limit %(k)s""",
        p,
    ).fetchall()
    return [{
        "tipo": tipo, "id": str(f["id"]), "titulo": f["titulo"], "contenido": f["contenido"],
        "version": f["version"], "detalle": f["extra"],
        "relevancia": round(float(max(f["sim"], min(f["rank"] * 10, 1.0))), 3),
    } for f in filas]


def _buscar_chunks(conn, p: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    vector = None
    if embeddings.disponible():
        try:
            vector = embeddings.a_pgvector(embeddings.generar([p["q"]])[0])
        except embeddings.EmbeddingsNoDisponibles:
            vector = None
    p = {**p, "v": vector, "n": max(20, p["k"] * 4)}
    conn.execute("set local hnsw.ef_search = 100")
    semantica = """
        sem as (
          select c.id, row_number() over (order by c.embedding <=> %(v)s::extensions.vector) r
          from chunks c join documentos d on d.id = c.documento_id
          where c.empresa_id = %(e)s and d.estado = 'aprobado' and c.embedding is not null
          order by c.embedding <=> %(v)s::extensions.vector limit %(n)s
        ),""" if vector else "sem as (select null::uuid id, null::bigint r where false),"
    filas = conn.execute(
        f"""with {semantica}
        lex as (
          select c.id, row_number() over (order by ts_rank_cd(c.tsv, tsq) desc) r
          from chunks c join documentos d on d.id = c.documento_id, {_TSQ}
          where c.empresa_id = %(e)s and d.estado = 'aprobado' and c.tsv @@ tsq
          order by ts_rank_cd(c.tsv, tsq) desc limit %(n)s
        ),
        fusion as (
          select coalesce(sem.id, lex.id) id,
                 coalesce(1.0 / (60 + sem.r), 0) + coalesce(1.0 / (60 + lex.r), 0) score,
                 sem.r is not null en_sem, lex.r is not null en_lex
          from sem full outer join lex on sem.id = lex.id
        )
        select c.id, c.contenido, c.orden, d.id documento_id, d.titulo, d.tipo, d.fuente, d.version,
               f.score, f.en_sem, f.en_lex
        from fusion f join chunks c on c.id = f.id join documentos d on d.id = c.documento_id
        order by f.score desc limit %(k)s""",
        p,
    ).fetchall()
    modo = "hibrido" if vector else "texto (sin embeddings)"
    return [{
        "tipo": "documento", "id": str(f["id"]), "titulo": f["titulo"], "contenido": f["contenido"],
        "version": f["version"], "detalle": {"documento_id": str(f["documento_id"]), "tipo_doc": f["tipo"],
                                             "fuente": f["fuente"], "fragmento": f["orden"] + 1},
        "relevancia": round(float(f["score"]) * 30, 3),  # 1.0 ≈ primero en ambas listas
    } for f in filas], modo
