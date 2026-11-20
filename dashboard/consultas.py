"""Consultas de lectura del panel. Todo filtrado por la empresa del admin que entró."""
from __future__ import annotations

from typing import Any

from logos import db

CONSULTAS = ("buscar_contexto", "obtener_definicion", "obtener_proceso", "obtener_reglas")
PROPUESTA_VIEJA_DIAS = 3

# Una fila por elemento de conocimiento, con columnas comunes
_CONOCIMIENTO = """
  select 'definicion' tipo, id, termino nombre, estado, version, actualizado_en, dueno, categoria detalle,
         left(definicion, 200) resumen, propuesta_id, empresa_id from definiciones
  union all
  select 'proceso', id, nombre, estado, version, actualizado_en, responsable, null,
         left(coalesce(descripcion, ''), 200), propuesta_id, empresa_id from procesos
  union all
  select 'regla', id, nombre, estado, version, actualizado_en, null, aplica_a,
         left(restriccion, 200), propuesta_id, empresa_id from reglas
  union all
  select 'documento', id, titulo, estado, version, actualizado_en, fuente, tipo,
         left(contenido, 200), null, empresa_id from documentos
"""


def nombre_empresa(empresa_id: str) -> str:
    with db.conexion() as conn:
        return conn.execute("select nombre from empresas where id = %s", (empresa_id,)).fetchone()["nombre"]


def resumen(empresa_id: str, dias: int) -> dict[str, Any]:
    p = {"e": empresa_id, "d": dias, "vieja": PROPUESTA_VIEJA_DIAS, "consultas": list(CONSULTAS)}
    with db.conexion() as conn:
        r = conn.execute(
            """select
                 (select count(*) from definiciones where empresa_id = %(e)s and estado = 'aprobado') definiciones,
                 (select count(*) from procesos where empresa_id = %(e)s and estado = 'aprobado') procesos,
                 (select count(*) from reglas where empresa_id = %(e)s and estado = 'aprobado') reglas,
                 (select count(*) from documentos where empresa_id = %(e)s and estado = 'aprobado') documentos,
                 (select count(*) from propuestas where empresa_id = %(e)s and estado = 'pendiente') pendientes,
                 (select count(*) from propuestas where empresa_id = %(e)s and estado = 'pendiente'
                    and creado_en < now() - make_interval(days => %(vieja)s)) pendientes_viejas,
                 (select count(*) from propuestas where empresa_id = %(e)s and estado = 'aprobada'
                    and revisado_en > now() - make_interval(days => %(d)s)) aprobadas,
                 (select count(*) from propuestas where empresa_id = %(e)s and estado = 'rechazada'
                    and revisado_en > now() - make_interval(days => %(d)s)) rechazadas,
                 (select count(*) from propuestas where empresa_id = %(e)s
                    and creado_en > now() - make_interval(days => %(d)s)) propuestas_nuevas,
                 (select count(*) from auditoria where empresa_id = %(e)s and accion = any(%(consultas)s)
                    and creado_en > now() - make_interval(days => %(d)s)) consultas,
                 (select count(*) from auditoria where empresa_id = %(e)s and accion = any(%(consultas)s)
                    and creado_en > now() - make_interval(days => %(d)s)
                    and coalesce((detalle ->> 'resultados')::int, 0) = 0 and not detalle ? 'error') sin_respuesta,
                 (select count(*) from chunks c join documentos d on d.id = c.documento_id
                    where c.empresa_id = %(e)s and d.estado = 'aprobado') chunks,
                 (select count(*) from chunks c join documentos d on d.id = c.documento_id
                    where c.empresa_id = %(e)s and d.estado = 'aprobado' and c.embedding is not null) chunks_indexados""",
            p,
        ).fetchone()
        r["consultas_por_dia"] = _por_dia(conn, """select creado_en from auditoria where empresa_id = %(e)s
                                                  and accion = any(%(consultas)s)""", p, min(dias, 14))
        r["propuestas_por_dia"] = _por_dia(conn, "select creado_en from propuestas where empresa_id = %(e)s", p,
                                           min(dias, 14))
    r["vigentes"] = r["definiciones"] + r["procesos"] + r["reglas"] + r["documentos"]
    r["salud"] = salud(r)
    return r


def _por_dia(conn, base: str, p: dict[str, Any], dias: int) -> list[int]:
    filas = conn.execute(
        f"""select g.dia::date dia, count(t.creado_en) n
            from generate_series(current_date - (%(n)s - 1), current_date, interval '1 day') g(dia)
            left join ({base}) t on t.creado_en::date = g.dia::date
            group by 1 order by 1""",
        {**p, "n": dias},
    ).fetchall()
    return [f["n"] for f in filas]


def salud(r: dict[str, Any]) -> dict[str, Any]:
    """Tres indicadores que se entienden solos y su promedio. Sin datos, el indicador no cuenta."""
    partes = []
    if r["chunks"]:
        partes.append(("Documentos indexados", round(100 * r["chunks_indexados"] / r["chunks"]),
                       f"{r['chunks_indexados']} de {r['chunks']} fragmentos con búsqueda por significado"))
    if r["consultas"]:
        resp = r["consultas"] - r["sin_respuesta"]
        partes.append(("Consultas con respuesta", round(100 * resp / r["consultas"]),
                       f"{resp} de {r['consultas']} consultas de agentes"))
    if r["pendientes"]:
        al_dia = r["pendientes"] - r["pendientes_viejas"]
        partes.append(("Propuestas al día",
                       round(100 * al_dia / r["pendientes"]),
                       (f"1 espera hace más de {PROPUESTA_VIEJA_DIAS} días" if r["pendientes_viejas"] == 1
                        else f"{r['pendientes_viejas']} esperan hace más de {PROPUESTA_VIEJA_DIAS} días")
                       if r["pendientes_viejas"] else f"ninguna espera más de {PROPUESTA_VIEJA_DIAS} días"))
    total = round(sum(p[1] for p in partes) / len(partes)) if partes else None
    return {"total": total, "partes": partes}


def sin_respuesta(empresa_id: str, dias: int, limite: int = 6) -> list[dict[str, Any]]:
    with db.conexion() as conn:
        return conn.execute(
            """select coalesce(detalle -> 'params' ->> 'query', detalle -> 'params' ->> 'termino',
                               detalle -> 'params' ->> 'nombre', detalle -> 'params' ->> 'aplica_a') consulta,
                      accion, count(*) veces, max(creado_en) ultima, array_agg(distinct actor) quienes
               from auditoria
               where empresa_id = %s and creado_en > now() - make_interval(days => %s)
                 and accion = any(%s) and coalesce((detalle ->> 'resultados')::int, 0) = 0
                 and not detalle ? 'error'
               group by 1, 2 order by veces desc, ultima desc limit %s""",
            (empresa_id, dias, list(CONSULTAS), limite),
        ).fetchall()


def mas_consultado(empresa_id: str, dias: int, limite: int = 6) -> list[dict[str, Any]]:
    with db.conexion() as conn:
        return conn.execute(
            """select d ->> 'tipo' tipo, d ->> 'id' id, max(d ->> 'titulo') titulo, count(*) veces
               from auditoria a, jsonb_array_elements(coalesce(a.detalle -> 'devueltos', '[]'::jsonb)) d
               where a.empresa_id = %s and a.creado_en > now() - make_interval(days => %s)
               group by 1, 2 order by veces desc, titulo limit %s""",
            (empresa_id, dias, limite),
        ).fetchall()


def actividad(empresa_id: str, limite: int = 200, filtro: str = "todo", actor: str = "") -> list[dict[str, Any]]:
    condicion = {
        "consultas": "and accion = any(%(consultas)s)",
        "cambios": "and accion in ('proponer_actualizacion', 'cargar_documento', 'aprobar_propuesta', 'rechazar_propuesta')",
        "accesos": "and accion in ('crear_token', 'entrar_dashboard')",
    }.get(filtro, "")
    with db.conexion() as conn:
        return conn.execute(
            f"""select id, actor, rol, accion, detalle, creado_en from auditoria
                where empresa_id = %(e)s {condicion} and (%(actor)s = '' or actor = %(actor)s)
                order by id desc limit %(n)s""",
            {"e": empresa_id, "consultas": list(CONSULTAS), "actor": actor, "n": limite},
        ).fetchall()


def actores(empresa_id: str) -> list[str]:
    with db.conexion() as conn:
        return [f["actor"] for f in conn.execute(
            "select distinct actor from auditoria where empresa_id = %s order by actor", (empresa_id,)).fetchall()]


def conocimiento(empresa_id: str, tipo: str, estado: str, q: str) -> list[dict[str, Any]]:
    with db.conexion() as conn:
        return conn.execute(
            f"""select * from ({_CONOCIMIENTO}) k
                where empresa_id = %(e)s
                  and (%(tipo)s = 'todo' or tipo = %(tipo)s)
                  and (%(estado)s = 'todos' or estado = %(estado)s)
                  and (%(q)s = '' or nombre ilike '%%' || %(q)s || '%%' or resumen ilike '%%' || %(q)s || '%%'
                       or coalesce(dueno, '') ilike '%%' || %(q)s || '%%'
                       or similarity(lower(nombre), lower(%(q)s)) > 0.3)
                order by case estado when 'aprobado' then 0 when 'borrador' then 1 else 2 end,
                         lower(nombre), version desc""",
            {"e": empresa_id, "tipo": tipo, "estado": estado, "q": q.strip()},
        ).fetchall()


def conteo_por_tipo(empresa_id: str, estado: str) -> dict[str, int]:
    with db.conexion() as conn:
        filas = conn.execute(
            f"""select tipo, count(*) n from ({_CONOCIMIENTO}) k where empresa_id = %s
                and (%s = 'todos' or estado = %s) group by tipo""", (empresa_id, estado, estado)).fetchall()
    return {f["tipo"]: f["n"] for f in filas}


TABLAS = {"definicion": ("definiciones", "termino"), "proceso": ("procesos", "nombre"),
          "regla": ("reglas", "nombre"), "documento": ("documentos", "titulo")}


def elemento(empresa_id: str, tipo: str, eid: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """El elemento y todas sus versiones (misma clave), con quién la aprobó."""
    tabla, clave = TABLAS[tipo]
    with db.conexion() as conn:
        fila = conn.execute(f"select * from {tabla} where id = %s and empresa_id = %s", (eid, empresa_id)).fetchone()
        if not fila:
            return None, []
        if tipo == "documento":
            versiones = conn.execute(
                """select d.id, d.version, d.estado, d.creado_en, d.actualizado_en, p.id propuesta_id,
                          p.propuesto_por, p.revisado_por, p.revisado_en, p.justificacion,
                          (select count(*) from chunks c where c.documento_id = d.id) chunks
                   from documentos d
                   left join propuestas p on p.empresa_id = d.empresa_id and p.contenido ->> 'documento_id' = d.id::text
                        and p.contenido ->> 'accion' = 'actualizar'
                   where d.empresa_id = %s and lower(d.titulo) = lower(%s)
                   order by d.estado = 'borrador' desc, d.version desc, d.creado_en desc""",
                (empresa_id, fila[clave]),
            ).fetchall()
        else:
            versiones = conn.execute(
                f"""select t.*, p.propuesto_por, p.revisado_por, p.revisado_en, p.justificacion
                    from {tabla} t left join propuestas p on p.id = t.propuesta_id
                    where t.empresa_id = %s and lower(t.{clave}) = lower(%s) order by t.version desc""",
                (empresa_id, fila[clave]),
            ).fetchall()
    return fila, versiones


def propuesta(empresa_id: str, pid: str) -> dict[str, Any] | None:
    with db.conexion() as conn:
        return conn.execute("select * from propuestas where id = %s and empresa_id = %s", (pid, empresa_id)).fetchone()


def propuestas(empresa_id: str, estado: str) -> list[dict[str, Any]]:
    with db.conexion() as conn:
        return conn.execute(
            """select * from propuestas where empresa_id = %s and (%s = 'todas' or estado = %s)
               order by case estado when 'pendiente' then 0 else 1 end,
                        case when estado = 'pendiente' then creado_en end asc,
                        coalesce(revisado_en, creado_en) desc limit 300""",
            (empresa_id, estado, estado),
        ).fetchall()


def conteo_propuestas(empresa_id: str) -> dict[str, int]:
    with db.conexion() as conn:
        filas = conn.execute("select estado, count(*) n from propuestas where empresa_id = %s group by estado",
                             (empresa_id,)).fetchall()
    c = {f["estado"]: f["n"] for f in filas}
    c["todas"] = sum(c.values())
    return c


def vigente_de(empresa_id: str, prop: dict[str, Any]) -> dict[str, Any] | None:
    """La versión aprobada hoy sobre la que actúa una propuesta (para el diff)."""
    c = prop["contenido"]
    tabla, clave = TABLAS[prop["tipo"]]
    valor = c.get(clave)
    if prop["tipo"] == "documento" and not valor and c.get("documento_id"):
        with db.conexion() as conn:
            d = conn.execute("select titulo from documentos where id = %s", (c["documento_id"],)).fetchone()
        valor = d["titulo"] if d else None
    if not valor:
        return None
    with db.conexion() as conn:
        return conn.execute(
            f"select * from {tabla} where empresa_id = %s and lower({clave}) = lower(%s) and estado = 'aprobado'",
            (empresa_id, valor)).fetchone()


def documento(empresa_id: str, did: str) -> dict[str, Any] | None:
    with db.conexion() as conn:
        return conn.execute("select * from documentos where id = %s and empresa_id = %s", (did, empresa_id)).fetchone()


def version_de(empresa_id: str, tipo: str, eid: str) -> dict[str, Any] | None:
    tabla, _ = TABLAS[tipo]
    with db.conexion() as conn:
        return conn.execute(f"select * from {tabla} where id = %s and empresa_id = %s", (eid, empresa_id)).fetchone()


def buscar(empresa_id: str, q: str) -> dict[str, list[dict[str, Any]]]:
    q = q.strip()
    if not q:
        return {"conocimiento": [], "propuestas": [], "actividad": []}
    p = {"e": empresa_id, "q": q}
    with db.conexion() as conn:
        conocimiento = conn.execute(
            f"""select tipo, id, nombre, estado, version, resumen,
                       greatest(similarity(lower(nombre), lower(%(q)s)),
                                case when nombre ilike '%%' || %(q)s || '%%' then 0.9 else 0 end,
                                case when resumen ilike '%%' || %(q)s || '%%' then 0.5 else 0 end) puntaje
                from ({_CONOCIMIENTO}) k
                where empresa_id = %(e)s and (nombre ilike '%%' || %(q)s || '%%' or resumen ilike '%%' || %(q)s || '%%'
                      or similarity(lower(nombre), lower(%(q)s)) > 0.3)
                order by estado = 'aprobado' desc, puntaje desc limit 20""", p).fetchall()
        # documentos: también dentro del texto completo
        en_texto = conn.execute(
            """select 'documento' tipo, d.id, d.titulo nombre, d.estado, d.version,
                      ts_headline('spanish', d.contenido, plainto_tsquery('spanish', %(q)s),
                                  'MaxWords=24, MinWords=8, StartSel=⟦, StopSel=⟧') resumen
               from documentos d
               where d.empresa_id = %(e)s and to_tsvector('spanish', d.contenido) @@ plainto_tsquery('spanish', %(q)s)
               order by d.estado = 'aprobado' desc limit 10""", p).fetchall()
        props = conn.execute(
            """select id, tipo, contenido, estado, propuesto_por, creado_en from propuestas
               where empresa_id = %(e)s and (contenido::text ilike '%%' || %(q)s || '%%'
                     or justificacion ilike '%%' || %(q)s || '%%')
               order by creado_en desc limit 10""", p).fetchall()
        actividad_q = conn.execute(
            """select id, actor, accion, detalle, creado_en from auditoria
               where empresa_id = %(e)s and detalle::text ilike '%%' || %(q)s || '%%'
               order by id desc limit 10""", p).fetchall()
    vistos = {(c["tipo"], str(c["id"])) for c in conocimiento}
    for d in en_texto:
        if ("documento", str(d["id"])) not in vistos:
            conocimiento.append({**d, "resaltado": True})
    return {"conocimiento": conocimiento, "propuestas": props, "actividad": actividad_q}


def documentos(empresa_id: str, estado: str, q: str, tipo_doc: str = "") -> list[dict[str, Any]]:
    """Tabla de documentos con fragmentos, indexación y cuántas veces los devolvió Logos (30 días)."""
    with db.conexion() as conn:
        return conn.execute(
            """select d.id, d.titulo, d.tipo, d.fuente, d.estado, d.version, d.actualizado_en,
                      length(d.contenido) caracteres,
                      count(c.id) chunks, count(c.embedding) indexados,
                      (select count(*) from auditoria a,
                              jsonb_array_elements(coalesce(a.detalle -> 'devueltos', '[]'::jsonb)) x
                        where a.empresa_id = d.empresa_id and x ->> 'id' = d.id::text
                          and a.creado_en > now() - interval '30 days') consultas
               from documentos d left join chunks c on c.documento_id = d.id
               where d.empresa_id = %(e)s
                 and (%(estado)s = 'todos' or d.estado = %(estado)s)
                 and (%(tipo)s = '' or lower(coalesce(d.tipo, '')) = lower(%(tipo)s))
                 and (%(q)s = '' or d.titulo ilike '%%' || %(q)s || '%%' or d.contenido ilike '%%' || %(q)s || '%%'
                      or coalesce(d.fuente, '') ilike '%%' || %(q)s || '%%')
               group by d.id
               order by case d.estado when 'borrador' then 0 when 'aprobado' then 1 else 2 end,
                        lower(d.titulo), d.version desc""",
            {"e": empresa_id, "estado": estado, "q": q.strip(), "tipo": tipo_doc},
        ).fetchall()


def tipos_documento(empresa_id: str) -> list[str]:
    with db.conexion() as conn:
        return [f["tipo"] for f in conn.execute(
            "select distinct tipo from documentos where empresa_id = %s and tipo is not null order by 1",
            (empresa_id,)).fetchall()]
