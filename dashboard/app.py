"""Panel de administración de Logos.

  python -m dashboard.app            → http://127.0.0.1:4910
Se entra con un token de ADMIN. Todo lo que se hace acá pasa por la misma gobernanza que el MCP.
Diseño: consola de control oscura y densa — menú fijo a la izquierda, tablas compactas, acento verde.
"""
from __future__ import annotations

import difflib
import html
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from logos import db, gobernanza as g
from logos.gobernanza import AccesoDenegado, DatosInvalidos, Identidad

from . import consultas as c

COOKIE = "logos_admin"
SEGURO = os.environ.get("DASHBOARD_HTTPS", "") == "1"
ESTATICOS = Path(__file__).resolve().parent / "static"

e = html.escape

TIPOS = {  # singular, plural, ícono Lucide
    "definicion": ("Definición", "Definiciones", "book-a"),
    "proceso": ("Proceso", "Procesos", "list-checks"),
    "regla": ("Regla", "Reglas", "shield-check"),
    "documento": ("Documento", "Documentos", "file-text"),
}
CAMPOS = {"termino": "Término", "definicion": "Definición", "categoria": "Categoría", "dueno": "Dueño",
          "nombre": "Nombre", "descripcion": "Descripción", "pasos": "Pasos", "responsable": "Responsable",
          "restricciones": "Restricciones", "aplica_a": "Aplica a", "restriccion": "Regla"}
ESTADO_CHIP = {"aprobado": ("verde", "Vigente"), "borrador": ("ambar", "Borrador"), "obsoleto": ("gris", "Obsoleto"),
               "pendiente": ("ambar", "Pendiente"), "aprobada": ("verde", "Aprobada"), "rechazada": ("rojo", "Rechazada")}
ACCIONES = {
    "buscar_contexto": ("search", "buscó"), "obtener_definicion": ("book-a", "consultó la definición"),
    "obtener_proceso": ("list-checks", "consultó el proceso"), "obtener_reglas": ("shield-check", "consultó las reglas de"),
    "proponer_actualizacion": ("git-pull-request-arrow", "propuso un cambio"), "cargar_documento": ("upload", "cargó el documento"),
    "aprobar_propuesta": ("check", "aprobó"), "rechazar_propuesta": ("x", "rechazó"),
    "crear_token": ("key-round", "creó un acceso"), "entrar_dashboard": ("log-in", "entró al panel"),
    "listar_propuestas": ("inbox", "listó propuestas"),
}


# ================================================================ utilidades

def ic(nombre: str) -> str:
    return f'<i data-lucide="{nombre}"></i>'


def chip(clase: str, texto: str, icono: str | None = None) -> str:
    return f'<span class="chip {clase}">{ic(icono) if icono else ""}{e(texto)}</span>'


def chip_estado(estado: str) -> str:
    clase, texto = ESTADO_CHIP.get(estado, ("gris", estado))
    return chip(f"{clase} punto", texto)


def _dt(valor: Any) -> datetime:
    return datetime.fromisoformat(valor) if isinstance(valor, str) else valor


def hace(valor: Any) -> str:
    if not valor:
        return "—"
    d = datetime.now(timezone.utc) - _dt(valor)
    if d.days >= 30:
        return _dt(valor).astimezone().strftime("%d/%m/%Y")
    if d.days >= 2:
        return f"hace {d.days} días"
    if d.days == 1:
        return "ayer"
    if d.seconds >= 3600:
        return f"hace {d.seconds // 3600} h"
    return f"hace {max(1, d.seconds // 60)} min"


def fecha(valor: Any) -> str:
    return _dt(valor).astimezone().strftime("%d/%m/%Y %H:%M") if valor else "—"


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def dia_largo(valor: Any) -> str:
    d = _dt(valor).astimezone()
    hoy = datetime.now().astimezone().date()
    if d.date() == hoy:
        return "Hoy"
    if (hoy - d.date()).days == 1:
        return "Ayer"
    return f"{DIAS[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]}"


def plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def iniciales(texto: str) -> str:
    palabras = [p for p in texto.replace("(", " ").split() if p[:1].isalpha() and p.lower() not in ("de", "del", "la")]
    return "".join(p[0] for p in palabras[:2]).upper() or "L"


def minibarras(valores: list[int]) -> str:
    if not valores or not any(valores):
        return ""
    tope = max(valores)
    return '<span class="mini">' + "".join(
        f'<i style="height:{max(3, round(18 * v / tope))}px"></i>' for v in valores) + "</span>"


def barra(pct: int | None) -> str:
    pct = pct or 0
    clase = "mal" if pct < 60 else "medio" if pct < 85 else ""
    return f'<div class="barra {clase}"><i style="width:{pct}%"></i></div>'


def valor_html(v: Any) -> str:
    if v is None or v == "" or v == []:
        return '<span class="tenue">—</span>'
    if isinstance(v, list):
        return "<ol>" + "".join(f"<li>{e(_txt(x))}</li>" for x in v) + "</ol>"
    return e(str(v))


def _txt(x: Any) -> str:
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)


def nombre_propuesta(p: dict[str, Any]) -> str:
    cont = p["contenido"]
    clave = c.TABLAS[p["tipo"]][1]
    return cont.get(clave) or cont.get("titulo") or "Sin nombre"


def cambio_propuesta(p: dict[str, Any], vigente: dict[str, Any] | None) -> tuple[str, str]:
    """(clase de chip, texto) de qué hace la propuesta."""
    if p["contenido"].get("accion") == "obsoletar":
        return "rojo", "Baja"
    if p["estado"] != "pendiente":
        return "gris", "Cambio"
    if vigente:
        return "gris", f"v{vigente['version']} → v{vigente['version'] + 1}"
    return "verde", "Nuevo"


def _es_vieja(p: dict[str, Any]) -> bool:
    return p["estado"] == "pendiente" and (datetime.now(timezone.utc) - _dt(p["creado_en"])).days >= c.PROPUESTA_VIEJA_DIAS


def _tipo(t: str) -> str:
    return f'<span class="tipo">{ic(TIPOS[t][2])}{TIPOS[t][0]}</span>'


# ================================================================ sesión

def _ident(request: Request) -> Identidad | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        ident = g.identificar(token)
    except AccesoDenegado:
        return None
    return ident if ident.es_admin else None


def protegido(fn):
    async def envoltura(request: Request) -> Response:
        ident = _ident(request)
        if not ident:
            return RedirectResponse("/entrar", status_code=303)
        return await fn(request, ident)
    return envoltura


async def entrar(request: Request) -> Response:
    error = ""
    if request.method == "POST":
        form = await request.form()
        token = str(form.get("token", "")).strip()
        try:
            ident = g.identificar(token)
            if not ident.es_admin:
                raise AccesoDenegado("Ese token es de un agente. El panel es solo para administradores.")
            g.auditar(ident, "entrar_dashboard", {})
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=SEGURO, max_age=60 * 60 * 12)
            return r
        except AccesoDenegado as ex:
            error = f'<p class="error">{e(str(ex))}</p>'
    cuerpo = f"""<main class="entrar"><div class="panel">
      <div class="marca"><span class="marca-logo">{ic("library")}</span>Logos</div>
      <h1>Entrar al panel</h1>
      <p>Control del conocimiento oficial de la empresa: lo que leen los agentes se revisa y se aprueba acá.</p>
      <form method="post" class="campos">
        <label>Token de administrador<input name="token" type="password" autocomplete="off" required autofocus
          placeholder="lgs_adm_…"></label>
        {error}
        <button class="btn ancho-total">Entrar</button>
      </form></div></main>"""
    return HTMLResponse(_documento("Entrar", cuerpo))


async def salir(request: Request) -> Response:
    r = RedirectResponse("/entrar", status_code=303)
    r.delete_cookie(COOKIE)
    return r


# ================================================================ layout

def _documento(titulo: str, cuerpo: str) -> str:
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(titulo)} · Logos</title>
<meta name="color-scheme" content="dark">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/static/logos.css"></head>
<body>{cuerpo}
<script src="/static/lucide.min.js"></script>
<script>
lucide.createIcons();
document.addEventListener('keydown', ev => {{
  const q = document.querySelector('.lat-buscar input');
  if (ev.key === '/' && q && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {{
    ev.preventDefault(); document.querySelector('.lateral').classList.add('abierto'); q.focus(); }}
}});
document.querySelectorAll('[data-abrir]').forEach(tr => tr.addEventListener('click', ev => {{
  if (!ev.target.closest('a, button, form') && !getSelection().toString()) location.href = tr.dataset.abrir;
}}));
const menu = document.querySelector('.abrir-menu');
if (menu) menu.addEventListener('click', () => document.querySelector('.lateral').classList.toggle('abierto'));
document.querySelectorAll('select[data-auto]').forEach(s => s.addEventListener('change', () => s.form.submit()));
</script></body></html>"""


def _pagina(request: Request, ident: Identidad, titulo: str, cuerpo: str, activa: str = "") -> HTMLResponse:
    empresa = c.nombre_empresa(ident.empresa_id)
    r = c.resumen(ident.empresa_id, 7)
    s = r["salud"]

    def link(clave: str, href: str, icono: str, texto: str, cuenta: str = "", aviso: bool = False) -> str:
        n = f'<span class="cuenta{" aviso" if aviso else ""}">{cuenta}</span>' if cuenta else ""
        return f'<a href="{href}" class="{"activo" if clave == activa else ""}">{ic(icono)}{texto}{n}</a>'
    nav = (link("overview", "/", "layout-dashboard", "Overview") +
           link("conocimiento", "/conocimiento", "book-open", "Conocimiento", str(r["vigentes"])) +
           link("propuestas", "/propuestas", "git-pull-request-arrow", "Propuestas",
                str(r["pendientes"]) if r["pendientes"] else "", aviso=bool(r["pendientes"])) +
           link("documentos", "/documentos", "file-text", "Documentos", str(r["documentos"])) +
           link("actividad", "/actividad", "activity", "Actividad"))
    acciones = (link("cargar", "/cargar", "upload", "Cargar documento") +
                link("buscar", "/buscar", "search", "Búsqueda global"))
    if s["total"] is None:
        salud = '<div class="fila-sm"><span>Salud</span><b>—</b></div>' + barra(0) + "<small>Todavía sin datos.</small>"
    else:
        peor = min(s["partes"], key=lambda p: p[1])
        nota = f"{peor[0]}: {peor[1]} %" if peor[1] < 100 else "Todo al día."
        salud = f'<div class="fila-sm"><span>Salud</span><b>{s["total"]} %</b></div>{barra(s["total"])}<small>{e(nota)}</small>'
    q = e(request.query_params.get("q", "")) if request.url.path == "/buscar" else ""
    marco = f"""<div class="app">
  <aside class="lateral">
    <div class="lat-cab">
      <a class="marca" href="/"><span class="marca-logo">{ic("library")}</span>Logos</a>
      <button class="btn-icono abrir-menu" aria-label="Menú">{ic("menu")}</button>
    </div>
    <div class="lat-cuerpo">
      <div class="espacio"><span class="espacio-ini">{e(iniciales(empresa))}</span>
        <div><b>{e(empresa)}</b><small>{plural(r["vigentes"], "elemento vigente", "elementos vigentes")}</small></div></div>
      <form class="lat-buscar" action="/buscar" role="search">{ic("search")}
        <input name="q" value="{q}" placeholder="Buscar…" aria-label="Buscar en todo"><kbd>/</kbd></form>
      <nav class="nav"><div class="nav-titulo">Gestión</div>{nav}</nav>
      <nav class="nav"><div class="nav-titulo">Acciones</div>{acciones}</nav>
      <div class="lat-pie">
        <a class="salud-mini" href="/#salud">{salud}</a>
        <div class="usuario"><span class="avatar">{e(iniciales(ident.nombre))}</span>
          <div><b>{e(ident.nombre)}</b><small>Administrador</small></div>
          <a class="btn-icono" href="/salir" title="Salir" aria-label="Salir">{ic("log-out")}</a></div>
      </div>
    </div>
  </aside>
  <main class="principal">{cuerpo}</main>
</div>"""
    return HTMLResponse(_documento(titulo, marco))


def _cabecera(titulo: str, sub: str = "", derecha: str = "", migas: list[tuple[str, str]] | None = None) -> str:
    m = ""
    if migas:
        m = '<nav class="migas">' + '<span>/</span>'.join(f'<a href="{h}">{e(t)}</a>' for h, t in migas) + "</nav>"
    return f"""<header class="cab"><div>{m}<h1>{titulo}</h1>{f'<div class="cab-sub">{sub}</div>' if sub else ""}</div>
      {f'<div class="cab-der">{derecha}</div>' if derecha else ""}</header>"""


def _panel(titulo: str, cuerpo: str, cuenta: int | str | None = None, sub: str = "", acciones: str = "",
           id_: str = "", clase: str = "", cuenta_aviso: bool = False) -> str:
    n = f'<span class="n{" aviso" if cuenta_aviso else ""}">{cuenta}</span>' if cuenta not in (None, "", 0) else ""
    return f"""<section class="panel {clase}"{f' id="{id_}"' if id_ else ""}>
      <div class="panel-cab"><h2>{titulo}</h2>{n}{f'<span class="panel-sub">{sub}</span>' if sub else ""}
        {f'<div class="panel-acc">{acciones}</div>' if acciones else ""}</div>{cuerpo}</section>"""


def _vacio(texto: str, icono: str = "circle-check") -> str:
    return f'<div class="vacio">{ic(icono)}{e(texto)}</div>'


def _segm(opciones: list[tuple[str, str, str, Any]], activa: str) -> str:
    """Control segmentado: (clave, href, texto, cuenta|None)."""
    return '<div class="segm">' + "".join(
        f'<a href="{h}" class="{"activo" if k == activa else ""}">{t}{f" <span>{n}</span>" if n is not None else ""}</a>'
        for k, h, t, n in opciones) + "</div>"


# ================================================================ overview

@protegido
async def overview(request: Request, ident: Identidad) -> Response:
    dias = 30 if request.query_params.get("periodo") == "30" else 7
    r = c.resumen(ident.empresa_id, dias)
    pendientes = c.propuestas(ident.empresa_id, "pendiente")
    sin_resp = c.sin_respuesta(ident.empresa_id, dias)
    top = c.mas_consultado(ident.empresa_id, dias)
    eventos = c.actividad(ident.empresa_id, 12)
    s = r["salud"]

    ahora = datetime.now().astimezone()
    partes = [f"<span>{DIAS[ahora.weekday()].capitalize()} {ahora.day} de {MESES[ahora.month - 1]}</span>"]
    if r["pendientes"]:
        partes.append(f'<b>{plural(r["pendientes"], "propuesta espera revisión", "propuestas esperan revisión")}</b>')
    if r["sin_respuesta"]:
        partes.append(f'<b class="mal">{plural(r["sin_respuesta"], "consulta sin respuesta", "consultas sin respuesta")}</b>')
    if len(partes) == 1:
        partes.append("<span>Todo revisado</span>")
    sub = '<span class="sep"></span>'.join(partes)
    periodo = _segm([("7", "/?periodo=7", "7 días", None), ("30", "/?periodo=30", "30 días", None)], str(dias))
    derecha = periodo + f'<a class="btn-borde" href="/cargar">{ic("upload")}Cargar documento</a>'

    # --- métricas
    sin_r = (f'<span class="mal">{plural(r["sin_respuesta"], "sin respuesta", "sin respuesta")}</span>'
             if r["sin_respuesta"] else '<span class="bien">todas con respuesta</span>' if r["consultas"]
             else "sin consultas")
    viejas = (f'<span class="espera-vieja">{r["pendientes_viejas"]} con más de {c.PROPUESTA_VIEJA_DIAS} días</span>'
              if r["pendientes_viejas"] else f'{plural(r["propuestas_nuevas"], "nueva", "nuevas")} en {dias} d')
    metricas = f"""<section class="metricas">
      <a class="metrica" href="/conocimiento"><span class="m-et">{ic("book-open")}Conocimiento vigente</span>
        <span class="m-val">{r['vigentes']}</span>
        <span class="m-pie">{r['definiciones']} def · {r['procesos']} proc · {r['reglas']} reglas · {r['documentos']} docs</span></a>
      <a class="metrica{' aviso' if r['pendientes'] else ''}" href="/propuestas"><span class="m-et">{ic("git-pull-request-arrow")}Propuestas pendientes</span>
        <span class="m-val">{r['pendientes']}</span><span class="m-pie">{viejas}{minibarras(r['propuestas_por_dia'])}</span></a>
      <a class="metrica" href="/propuestas?estado=aprobada"><span class="m-et">{ic("check-check")}Revisadas · {dias} d</span>
        <span class="m-val">{r['aprobadas'] + r['rechazadas']}</span>
        <span class="m-pie">{r['aprobadas']} aprobadas · {r['rechazadas']} rechazadas</span></a>
      <a class="metrica" href="/actividad?filtro=consultas"><span class="m-et">{ic("bot")}Consultas de agentes · {dias} d</span>
        <span class="m-val">{r['consultas']}</span><span class="m-pie">{sin_r}{minibarras(r['consultas_por_dia'])}</span></a>
      <a class="metrica{' mal' if (s['total'] or 100) < 60 else ''}" href="#salud"><span class="m-et">{ic("heart-pulse")}Salud</span>
        <span class="m-val">{f"{s['total']}<small>%</small>" if s['total'] is not None else "—"}</span>
        <span class="m-pie">{barra(s['total'])}</span></a>
    </section>"""

    # --- propuestas pendientes (lo más importante)
    if pendientes:
        trs = []
        for p in pendientes[:8]:
            vig = c.vigente_de(ident.empresa_id, p)
            clase, cambio = cambio_propuesta(p, vig)
            vieja = _es_vieja(p)
            trs.append(f"""<tr data-abrir="/propuesta/{p['id']}">
              <td>{_tipo(p['tipo'])}</td>
              <td class="texto"><a class="nombre" href="/propuesta/{p['id']}">{e(nombre_propuesta(p))}</a>
                <small>{e(p['justificacion'] or '')}</small></td>
              <td>{chip(clase, cambio)}</td>
              <td class="ocultar-celu">{e(p['propuesto_por'] or '—')}</td>
              <td class="ocultar-celu{' espera-vieja' if vieja else ''}" title="{fecha(p['creado_en'])}">{hace(p['creado_en'])}</td>
              <td class="der"><a class="btn-borde chico" href="/propuesta/{p['id']}">Revisar</a></td></tr>""")
        cuerpo_p = f"""<div class="tabla-scroll"><table class="tabla"><thead><tr><th>Tipo</th><th>Propuesta</th><th>Cambio</th>
          <th class="ocultar-celu">Propuso</th><th class="ocultar-celu">Espera</th><th></th></tr></thead>
          <tbody>{''.join(trs)}</tbody></table></div>"""
    else:
        cuerpo_p = _vacio("No hay propuestas esperando. Todo lo que leen los agentes está revisado.")
    revisar = _panel("Propuestas pendientes", cuerpo_p, cuenta=len(pendientes), cuenta_aviso=bool(pendientes),
                     sub="Los agentes no ven nada de esto hasta que lo apruebes.",
                     clase="destacado" if pendientes else "",
                     acciones=f'<a class="enlace" href="/propuestas">Ver todas {ic("arrow-right")}</a>')

    actividad = _panel("Actividad reciente", f'<div class="log compacto">{_log(eventos, agrupar=False)}</div>'
                       if eventos else _vacio("Sin actividad todavía.", "info"),
                       acciones=f'<a class="enlace" href="/actividad">Ver registro {ic("arrow-right")}</a>')

    # --- columna derecha
    if s["partes"]:
        filas_s = "".join(f"""<div class="indicador"><div><span>{e(nombre)}</span><b>{pct} %</b></div>
            {barra(pct)}<small>{e(det)}</small></div>""" for nombre, pct, det in s["partes"])
        cuerpo_s = f"""<div class="panel-cuerpo"><div class="salud-total"><b>{s['total']} %</b>
            <span>promedio de {plural(len(s['partes']), 'indicador', 'indicadores')}</span></div>
            <div class="indicadores">{filas_s}</div></div>"""
    else:
        cuerpo_s = _vacio("Todavía no hay datos para medir.", "info")
    salud = _panel("Salud del conocimiento", cuerpo_s, id_="salud")

    if sin_resp:
        filas_n = "".join(f"""<a class="fila" href="/cargar?titulo={quote(x['consulta'] or '')}" title="Cargar un documento sobre esto">
            <span class="ic mal">{ic("search-x")}</span>
            <div><h4>“{e(x['consulta'] or '')}”</h4><p>{e(', '.join(x['quienes']))} · {hace(x['ultima'])}</p></div>
            <span class="der mono">{x['veces']}×</span></a>""" for x in sin_resp)
        cuerpo_n = f'<div class="filas">{filas_n}</div>'
    else:
        cuerpo_n = _vacio("Los agentes encontraron todo lo que buscaron.")
    no_encontrado = _panel("Sin respuesta", cuerpo_n, cuenta=len(sin_resp), sub="candidatos a cargar")

    if top:
        tope = top[0]["veces"]
        lis = "".join(f"""<li><a href="/k/{x['tipo']}/{e(x['id'])}">{ic(TIPOS.get(x['tipo'], ('', '', 'file'))[2])}
            <span>{e(x['titulo'] or '')}</span>{barra(round(100 * x['veces'] / tope))}<em>{x['veces']}</em></a></li>"""
                      for x in top)
        cuerpo_t = f'<ul class="ranking">{lis}</ul>'
    else:
        cuerpo_t = _vacio("Todavía no hay consultas en este período.", "info")
    consultado = _panel("Más consultado", cuerpo_t, sub=f"últimos {dias} días")

    cuerpo = (_cabecera("Overview", sub, derecha) + metricas +
              f'<div class="grilla"><div class="col">{revisar}{actividad}</div>'
              f'<div class="col">{salud}{no_encontrado}{consultado}</div></div>')
    return _pagina(request, ident, "Overview", cuerpo, "overview")


def _log(eventos: list[dict[str, Any]], agrupar: bool = True) -> str:
    """Registro de auditoría como filas densas. agrupar=True: por día con hora; False: compacto con 'hace'."""
    salida, dia_actual = [], None
    for ev in eventos:
        if agrupar:
            d = dia_largo(ev["creado_en"])
            if d != dia_actual:
                salida.append(f'<div class="log-dia">{d}</div>')
                dia_actual = d
        icono, verbo = ACCIONES.get(ev["accion"], ("dot", ev["accion"]))
        det = ev["detalle"] or {}
        params = det.get("params") or {}
        objeto = params.get("query") or params.get("termino") or params.get("nombre") or params.get("aplica_a") or det.get("titulo") or ""
        extra, clase, enlace = "", "", ""
        if "error" in det:
            extra, clase = f"No se permitió: {det['error']}", "mal"
        elif ev["accion"] in c.CONSULTAS:
            dev = det.get("devueltos") or []
            if det.get("resultados") == 0:
                extra, clase = "Sin resultados", "mal"
            elif dev:
                extra = f"{len(dev)} → " + ", ".join(d["titulo"] for d in dev[:3]) + ("…" if len(dev) > 3 else "")
        elif ev["accion"] in ("proponer_actualizacion", "cargar_documento", "aprobar_propuesta", "rechazar_propuesta"):
            clase = "cambio"
            if det.get("propuesta_id"):
                enlace = f'/propuesta/{det["propuesta_id"]}'
            tipo = det.get("tipo")
            if ev["accion"] == "aprobar_propuesta" and det.get("version"):
                extra = f"{TIPOS.get(tipo, ('Elemento',))[0]} · queda v{det['version']}"
            elif ev["accion"] == "rechazar_propuesta":
                extra = f"Motivo: {det.get('motivo', '—')}"
            elif tipo in TIPOS:
                extra = TIPOS[tipo][0]
            if det.get("chunks"):
                extra = plural(det["chunks"], "fragmento", "fragmentos")
            if ev["accion"] in ("aprobar_propuesta", "rechazar_propuesta") and not objeto:
                objeto = "una propuesta"
        obj = f" <q>{e(str(objeto))}</q>" if objeto else ""
        texto = f"<b>{e(ev['actor'])}</b> {e(verbo)}{obj}"
        etiqueta = "a" if enlace else "div"
        href = f' href="{enlace}"' if enlace else ""
        if agrupar:
            hora = _dt(ev["creado_en"]).astimezone().strftime("%H:%M")
            salida.append(f"""<{etiqueta} class="log-fila"{href}><time title="{fecha(ev['creado_en'])}">{hora}</time>
              <span class="log-ic {clase}">{ic(icono)}</span><span class="log-txt">{texto}</span>
              <span class="log-det {clase if clase == 'mal' else ''}">{e(extra)}</span></{etiqueta}>""")
        else:
            peq = f'<small class="{clase if clase == "mal" else ""}">{e(extra)}</small>' if extra else ""
            salida.append(f"""<{etiqueta} class="log-fila"{href}><span class="log-ic {clase}">{ic(icono)}</span>
              <span class="log-txt">{texto}{peq}</span>
              <time title="{fecha(ev['creado_en'])}">{hace(ev['creado_en']).replace('hace ', '')}</time></{etiqueta}>""")
    return "".join(salida)


# ================================================================ propuestas

@protegido
async def propuestas(request: Request, ident: Identidad) -> Response:
    estado = request.query_params.get("estado", "pendiente")
    if estado not in ("pendiente", "aprobada", "rechazada", "todas"):
        estado = "pendiente"
    tipo = request.query_params.get("tipo", "")
    tipo = tipo if tipo in TIPOS else ""
    conteo = c.conteo_propuestas(ident.empresa_id)
    lista = [p for p in c.propuestas(ident.empresa_id, estado) if not tipo or p["tipo"] == tipo]

    def url(**cambios: str) -> str:
        base = {"estado": estado, "tipo": tipo}
        base.update(cambios)
        return "/propuestas?" + urlencode({k: v for k, v in base.items() if v})
    pest = _segm([(k, url(estado=k), t, conteo.get(k, 0)) for k, t in
                  (("pendiente", "Pendientes"), ("aprobada", "Aprobadas"), ("rechazada", "Rechazadas"), ("todas", "Todas"))],
                 estado)
    opciones = '<option value="">Todos los tipos</option>' + "".join(
        f'<option value="{k}"{" selected" if k == tipo else ""}>{v[1]}</option>' for k, v in TIPOS.items())
    filtros = f"""<form class="barra-filtros" method="get" action="/propuestas">{pest}
      <input type="hidden" name="estado" value="{estado}">
      <select name="tipo" data-auto aria-label="Tipo">{opciones}</select>
      <span class="der">{plural(len(lista), "propuesta", "propuestas")}</span></form>"""
    filas = []
    for p in lista:
        vig = c.vigente_de(ident.empresa_id, p) if p["estado"] == "pendiente" else None
        clase, cambio = cambio_propuesta(p, vig)
        revisado = (f"{e(p['revisado_por'] or '')}<small>{hace(p['revisado_en'])}</small>" if p["revisado_en"]
                    else '<span class="tenue">—</span>')
        vieja = _es_vieja(p)
        filas.append(f"""<tr data-abrir="/propuesta/{p['id']}">
          <td>{_tipo(p['tipo'])}</td>
          <td class="texto"><a class="nombre" href="/propuesta/{p['id']}">{e(nombre_propuesta(p))}</a><small>{e(p['justificacion'] or '')}</small></td>
          <td>{chip(clase, cambio)}</td>
          <td class="ocultar-celu">{e(p['propuesto_por'] or '—')}<small class="{'espera-vieja' if vieja else ''}">{hace(p['creado_en'])}</small></td>
          <td class="ocultar-celu">{revisado}</td>
          <td>{chip_estado(p['estado'])}</td></tr>""")
    tabla = f"""<div class="tabla-scroll"><table class="tabla"><thead><tr><th>Tipo</th><th>Propuesta</th><th>Cambio</th>
      <th class="ocultar-celu">Propuso</th><th class="ocultar-celu">Revisó</th><th>Estado</th></tr></thead>
      <tbody>{''.join(filas)}</tbody></table></div>""" if filas else _vacio("No hay propuestas con estos filtros.", "inbox")
    sub = ("Las pendientes no las leen los agentes hasta que las apruebes." if estado == "pendiente"
           else "Cada propuesta queda registrada con quién la hizo y quién la revisó.")
    cuerpo = _cabecera("Propuestas", f"<span>{sub}</span>")
    cuerpo += f'<section class="panel">{filtros}{tabla}</section>'
    return _pagina(request, ident, "Propuestas", cuerpo, "propuestas")


@protegido
async def propuesta(request: Request, ident: Identidad) -> Response:
    try:
        p = c.propuesta(ident.empresa_id, request.path_params["pid"])
    except Exception:  # id mal formado
        p = None
    if not p:
        return _pagina(request, ident, "No existe", _cabecera("No existe") + _vacio("No existe esa propuesta.", "circle-alert"),
                       "propuestas")
    pendiente = p["estado"] == "pendiente"
    vigente = c.vigente_de(ident.empresa_id, p) if pendiente else None
    resultado = None
    if not pendiente and p.get("resultado_id") and p["contenido"].get("accion") != "obsoletar":
        # para una ya aprobada, comparar contra la versión anterior a la que creó
        resultado = c.version_de(ident.empresa_id, p["tipo"], str(p["resultado_id"]))
        if resultado and resultado["version"] > 1:
            tabla, clave = c.TABLAS[p["tipo"]]
            with db.conexion() as conn:
                vigente = conn.execute(
                    f"select * from {tabla} where empresa_id = %s and lower({clave}) = lower(%s) and version = %s",
                    (ident.empresa_id, resultado[clave], resultado["version"] - 1)).fetchone()
    tipo = p["tipo"]
    cont = dict(p["contenido"])
    accion = cont.pop("accion", "actualizar")

    # --- qué cambia
    if tipo == "documento":
        diff, mas, menos, meta = _diff_documento(ident, cont, vigente, accion)
        resumen_cambio = f'<span class="resumen-cambio"><span class="mas">+{mas}</span><span class="menos">−{menos}</span> líneas</span>'
    else:
        diff, n = _diff_campos(tipo, cont, vigente, accion)
        meta = {}
        resumen_cambio = f'<span class="panel-sub">{plural(n, "campo cambia", "campos cambian")}</span>'
    v_antes = vigente["version"] if vigente else None
    if accion == "obsoletar":
        base = f'<span class="version-flecha">v{v_antes}{ic("arrow-right")}baja</span>' if v_antes else "Baja"
        que = "Dar de baja"
    elif v_antes:
        base = f'<span class="version-flecha">v{v_antes}{ic("arrow-right")}v{v_antes + 1}</span>'
        que = "Nueva versión"
    else:
        base = '<span class="version-flecha">—' + ic("arrow-right") + "v1</span>"
        que = "Alta nueva"

    notas = f'<div class="nota"><b>Justificación</b>{e(p["justificacion"] or "—")}</div>'
    if p.get("motivo_revision"):
        notas += f'<div class="nota roja"><b>Motivo del rechazo</b>{e(p["motivo_revision"])}</div>'
    acc = ""
    if p["estado"] == "aprobada" and p.get("resultado_id"):
        acc = f'<a class="enlace" href="/k/{tipo}/{p["resultado_id"]}">Ver cómo quedó {ic("arrow-right")}</a>'
    titulo_cambios = {"Dar de baja": "Qué se da de baja", "Alta nueva": "Qué se agrega"}.get(que, "Qué cambia")
    principal = _panel(titulo_cambios, notas + diff, sub="", acciones=resumen_cambio + acc)

    # --- lateral: decisión + detalles
    lado = ""
    if pendiente:
        lado += _panel("Decisión", f"""<div class="decision">
          <form method="post" action="/propuestas/{p['id']}/aprobar">
            <button class="btn ancho-total">{ic("check")}Aprobar y publicar</button></form>
          <span class="o">o</span>
          <form method="post" action="/propuestas/{p['id']}/rechazar">
            <input name="motivo" placeholder="Motivo del rechazo (obligatorio)" required>
            <button class="btn-borde peligro ancho-total">{ic("x")}Rechazar</button></form></div>""")
    revision = {"aprobada": "Aprobó", "rechazada": "Rechazó"}.get(p["estado"])
    datos = [("Estado", chip_estado(p["estado"])), ("Tipo", _tipo(tipo)), ("Acción", e(que)), ("Versión", base),
             ("Propuso", f"{e(p['propuesto_por'] or '—')}<small>{fecha(p['creado_en'])}</small>")]
    if revision:
        datos.append((revision, f"{e(p['revisado_por'] or '—')}<small>{fecha(p['revisado_en'])}</small>"))
    for k, v in meta.items():
        datos.append((k, e(str(v)) if v not in (None, "") else '<span class="tenue">—</span>'))
    datos.append(("ID", f'<span class="mono tenue">{str(p["id"])[:8]}</span>'))
    lado += _panel("Detalles", '<dl class="datos">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in datos) + "</dl>")

    sub = (f"{chip_estado(p['estado'])}<span>{TIPOS[tipo][0]}</span><span class=sep></span>"
           f"<span>propuso <b>{e(p['propuesto_por'] or '—')}</b></span><span class=sep></span>"
           f"<span title='{fecha(p['creado_en'])}'>{hace(p['creado_en'])}</span>")
    derecha = ""
    if pendiente:
        derecha = (f'<form method="post" action="/propuestas/{p["id"]}/aprobar">'
                   f'<button class="btn">{ic("check")}Aprobar</button></form>')
    cab = _cabecera(e(nombre_propuesta(p)), sub, derecha,
                    migas=[("/propuestas", "Propuestas"), (f"/propuesta/{p['id']}", str(p["id"])[:8])])
    cuerpo = cab + f'<div class="detalle"><div class="col">{principal}</div><aside class="lado">{lado}</aside></div>'
    return _pagina(request, ident, nombre_propuesta(p), cuerpo, "propuestas")


def _ln(signo: str, texto: str, clase: str = "") -> str:
    return f'<div class="ln {clase}"><span class="sg">{signo}</span><span class="tx">{e(texto) or "&nbsp;"}</span></div>'


def _lineas(v: Any) -> list[str]:
    if v in (None, "", []):
        return []
    if isinstance(v, list):
        return [f"{i}. {_txt(x)}" for i, x in enumerate(v, 1)]
    return str(v).splitlines() or [str(v)]


def _diff_campos(tipo: str, cont: dict[str, Any], vigente: dict[str, Any] | None, accion: str) -> tuple[str, int]:
    """Un bloque por campo que cambia (− lo que sale, + lo que entra); los que no cambian, plegados."""
    esquema = g.ESQUEMAS[tipo]
    campos = esquema["requeridos"] + [k for k in esquema["opcionales"] if k not in esquema["requeridos"]]
    cambiados, iguales = [], []
    for k in campos:
        antes = vigente.get(k) if vigente else None
        if accion == "obsoletar":
            despues = None
        elif k == esquema["clave"] and vigente:
            despues = antes  # el término/nombre vigente se conserva
        else:
            despues = cont[k] if k in cont else antes  # lo que no se propone se hereda
        if antes in (None, "", []) and despues in (None, "", []):
            continue
        nombre = CAMPOS.get(k, k)
        if accion == "obsoletar":
            lineas, etiqueta = "".join(_ln("−", x, "sale") for x in _lineas(antes)), chip("rojo", "se da de baja")
        elif not vigente:
            lineas, etiqueta = "".join(_ln("+", x, "entra") for x in _lineas(despues)), chip("verde", "nuevo")
        elif antes == despues:
            iguales.append(f'<div class="campo-igual"><span>{nombre}</span><div>{valor_html(antes)}</div></div>')
            continue
        else:
            if isinstance(antes, list) and isinstance(despues, list):
                a, b = [_txt(x) for x in antes], [_txt(x) for x in despues]
                partes = []
                for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
                    if op == "equal":
                        partes += [_ln(" ", f"{j1 + n + 1}. {x}") for n, x in enumerate(b[j1:j2])]
                    else:
                        partes += [_ln("−", f"{i1 + n + 1}. {x}", "sale") for n, x in enumerate(a[i1:i2])]
                        partes += [_ln("+", f"{j1 + n + 1}. {x}", "entra") for n, x in enumerate(b[j1:j2])]
                lineas = "".join(partes)
            else:
                lineas = ("".join(_ln("−", x, "sale") for x in _lineas(antes)) +
                          "".join(_ln("+", x, "entra") for x in _lineas(despues)))
            if antes in (None, "", []):
                etiqueta = chip("verde", "agregado")
            elif despues in (None, "", []):
                etiqueta = chip("rojo", "quitado")
            else:
                etiqueta = chip("ambar", "modificado")
        cambiados.append(f'<div class="campo"><div class="campo-cab">{nombre}{etiqueta}</div>{lineas}</div>')
    salida = "".join(cambiados) or _vacio("La propuesta no cambia ningún campo.", "info")
    if iguales:
        salida += (f'<details class="iguales"><summary>{plural(len(iguales), "campo sin cambios", "campos sin cambios")}'
                   f'</summary>{"".join(iguales)}</details>')
    return f'<div class="cambios">{salida}</div>', len(cambiados)


def _dl(n_a: Any, n_b: Any, signo: str, texto: str, clase: str = "") -> str:
    return (f'<div class="dl {clase}"><span class="n">{n_a or ""}</span><span class="n">{n_b or ""}</span>'
            f'<span class="sg">{signo}</span><span class="tx">{e(texto) or "&nbsp;"}</span></div>')


def _diff_documento(ident: Identidad, cont: dict[str, Any], vigente: dict[str, Any] | None,
                    accion: str) -> tuple[str, int, int, dict[str, Any]]:
    """Diff unificado por líneas con numeración (antes / después). Devuelve html, +líneas, −líneas, metadatos."""
    nuevo = c.documento(ident.empresa_id, cont["documento_id"]) if cont.get("documento_id") else None
    if accion == "obsoletar":
        doc = vigente or nuevo
        lineas = (doc["contenido"] if doc else "").splitlines()
        cuerpo = "".join(_dl(i, "", "−", x, "sale") for i, x in enumerate(lineas, 1))
        aviso = '<div class="nota roja"><b>Al aprobar</b>Este documento deja de estar disponible para los agentes.</div>'
        return aviso + f'<div class="diff-doc" style="margin-top:14px">{cuerpo}</div>', 0, len(lineas), \
            {"Fuente": doc["fuente"] if doc else None}
    if not nuevo:
        return _vacio("No se encontró el documento propuesto.", "circle-alert"), 0, 0, {}
    meta = {"Tipo de doc.": nuevo["tipo"], "Fuente": nuevo["fuente"], "Fragmentos": cont.get("chunks") or 0}
    b = nuevo["contenido"].splitlines()
    if not vigente or str(vigente["id"]) == str(nuevo["id"]):
        cuerpo = "".join(_dl("", j, "+", x, "entra") for j, x in enumerate(b, 1))
        return f'<div class="diff-doc" style="margin-top:14px">{cuerpo}</div>', len(b), 0, meta
    a = vigente["contenido"].splitlines()
    salida, mas, menos = [], 0, 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            bloque = list(range(i2 - i1))
            mostrar = bloque if len(bloque) <= 6 else bloque[:2] + [None] + bloque[-2:]
            for k in mostrar:
                if k is None:
                    salida.append(_dl("", "", "", f"⋯ {len(bloque) - 4} líneas sin cambios", "salto"))
                else:
                    salida.append(_dl(i1 + k + 1, j1 + k + 1, " ", a[i1 + k]))
        else:
            salida += [_dl(i1 + n + 1, "", "−", x, "sale") for n, x in enumerate(a[i1:i2])]
            salida += [_dl("", j1 + n + 1, "+", x, "entra") for n, x in enumerate(b[j1:j2])]
            menos += i2 - i1
            mas += j2 - j1
    return f'<div class="diff-doc" style="margin-top:14px">{"".join(salida)}</div>', mas, menos, meta


async def decidir(request: Request) -> Response:
    ident = _ident(request)
    if not ident:
        return RedirectResponse("/entrar", status_code=303)
    pid = request.path_params["pid"]
    if request.path_params["accion"] not in ("aprobar", "rechazar"):
        return Response(status_code=404)
    try:
        if request.path_params["accion"] == "aprobar":
            g.aprobar(ident, pid)
        else:
            form = await request.form()
            g.rechazar(ident, pid, str(form.get("motivo", "")))
    except DatosInvalidos as ex:
        cuerpo = (_cabecera("No se pudo", migas=[("/propuestas", "Propuestas"), (f"/propuesta/{e(pid)}", "Volver")]) +
                  f'<div class="alerta roja">{ic("circle-alert")}<p>{e(str(ex))}</p></div>')
        r = _pagina(request, ident, "Error", cuerpo, "propuestas")
        r.status_code = 400
        return r
    return RedirectResponse(f"/propuesta/{pid}", status_code=303)


# ================================================================ conocimiento

ESTADOS = (("aprobado", "Vigentes"), ("borrador", "Borradores"), ("obsoleto", "Obsoletos"), ("todos", "Todos"))


@protegido
async def conocimiento(request: Request, ident: Identidad) -> Response:
    qp = request.query_params
    tipo = qp.get("tipo", "todo")
    tipo = tipo if tipo in ("todo", *TIPOS) else "todo"
    estado = qp.get("estado", "aprobado")
    estado = estado if estado in dict(ESTADOS) else "aprobado"
    q = qp.get("q", "")
    filas = c.conocimiento(ident.empresa_id, tipo, estado, q)
    conteo = c.conteo_por_tipo(ident.empresa_id, estado)

    def url(**cambios: str) -> str:
        base = {"tipo": tipo, "estado": estado, "q": q}
        base.update(cambios)
        return "/conocimiento?" + urlencode({k: v for k, v in base.items() if v})
    pest = _segm([("todo", url(tipo="todo"), "Todo", sum(conteo.values()))] +
                 [(k, url(tipo=k), v[1], conteo.get(k, 0)) for k, v in TIPOS.items()], tipo)
    opciones = "".join(f'<option value="{k}"{" selected" if k == estado else ""}>{t}</option>' for k, t in ESTADOS)
    filtros = f"""<form class="barra-filtros" method="get" action="/conocimiento">{pest}
      <input type="hidden" name="tipo" value="{e(tipo)}">
      <label class="campo-buscar">{ic("search")}<input name="q" value="{e(q)}" placeholder="Filtrar por nombre, texto o dueño"></label>
      <select name="estado" data-auto aria-label="Estado">{opciones}</select>
      <span class="der">{plural(len(filas), "resultado", "resultados")}</span></form>"""
    trs = "".join(f"""<tr data-abrir="/k/{f['tipo']}/{f['id']}" class="{'obsoleto' if f['estado'] == 'obsoleto' else ''}">
        <td>{_tipo(f['tipo'])}</td>
        <td class="texto"><a class="nombre" href="/k/{f['tipo']}/{f['id']}">{e(f['nombre'])}</a><small>{e(f['resumen'] or '')}</small></td>
        <td>{chip_estado(f['estado'])}</td>
        <td class="num">v{f['version']}</td>
        <td class="ocultar-celu" title="{fecha(f['actualizado_en'])}">{hace(f['actualizado_en'])}</td>
        <td class="ocultar-celu">{e(f['dueno'] or (f'Aplica a: {f["detalle"]}' if f['tipo'] == 'regla' and f['detalle'] else '—'))}</td></tr>"""
                  for f in filas)
    tabla = f"""<div class="tabla-scroll"><table class="tabla"><thead><tr><th>Tipo</th><th>Nombre</th><th>Estado</th>
      <th>Versión</th><th class="ocultar-celu">Actualizado</th><th class="ocultar-celu">Dueño / fuente</th></tr></thead>
      <tbody>{trs}</tbody></table></div>""" if filas else _vacio("No hay nada con esos filtros.", "search-x")
    cuerpo = _cabecera("Conocimiento", "<span>Lo vigente es lo único que leen los agentes. Para cambiarlo, aprobá una propuesta.</span>",
                       f'<a class="btn-borde" href="/cargar">{ic("upload")}Cargar documento</a>')
    cuerpo += f'<section class="panel">{filtros}{tabla}</section>'
    return _pagina(request, ident, "Conocimiento", cuerpo, "conocimiento")


# ================================================================ documentos

@protegido
async def documentos(request: Request, ident: Identidad) -> Response:
    qp = request.query_params
    estado = qp.get("estado", "aprobado")
    estado = estado if estado in dict(ESTADOS) else "aprobado"
    tipo_doc = qp.get("tipo", "")
    q = qp.get("q", "")
    filas = c.documentos(ident.empresa_id, estado, q, tipo_doc)
    todos = c.documentos(ident.empresa_id, "todos", "", "")
    conteo = {k: sum(1 for d in todos if d["estado"] == k) for k, _ in ESTADOS}
    conteo["todos"] = len(todos)

    def url(**cambios: str) -> str:
        base = {"estado": estado, "tipo": tipo_doc, "q": q}
        base.update(cambios)
        return "/documentos?" + urlencode({k: v for k, v in base.items() if v})
    pest = _segm([(k, url(estado=k), t, conteo.get(k, 0)) for k, t in ESTADOS], estado)
    opciones = '<option value="">Todos los tipos</option>' + "".join(
        f'<option{" selected" if t == tipo_doc else ""}>{e(t)}</option>' for t in c.tipos_documento(ident.empresa_id))
    filtros = f"""<form class="barra-filtros" method="get" action="/documentos">{pest}
      <input type="hidden" name="estado" value="{estado}">
      <label class="campo-buscar">{ic("search")}<input name="q" value="{e(q)}" placeholder="Título, contenido o fuente"></label>
      <select name="tipo" data-auto aria-label="Tipo de documento">{opciones}</select>
      <span class="der">{plural(len(filas), "documento", "documentos")}</span></form>"""
    trs = []
    for d in filas:
        pct = round(100 * d["indexados"] / d["chunks"]) if d["chunks"] else 0
        trs.append(f"""<tr data-abrir="/k/documento/{d['id']}" class="{'obsoleto' if d['estado'] == 'obsoleto' else ''}">
          <td class="texto"><span class="tipo"><a class="nombre" href="/k/documento/{d['id']}">{e(d['titulo'])}</a></span>
            <small>{plural(d['caracteres'], 'carácter', 'caracteres')}</small></td>
          <td>{e(d['tipo'] or '—')}</td>
          <td>{chip_estado(d['estado'])}</td>
          <td class="num">v{d['version']}</td>
          <td class="ocultar-celu"><span class="ind">{barra(pct)}<span class="num">{d['indexados']}/{d['chunks']}</span></span></td>
          <td class="ocultar-celu num der">{d['consultas'] or '<span class="tenue">0</span>'}</td>
          <td class="ocultar-celu">{e(d['fuente'] or '—')}</td>
          <td class="ocultar-celu" title="{fecha(d['actualizado_en'])}">{hace(d['actualizado_en'])}</td></tr>""")
    tabla = f"""<div class="tabla-scroll"><table class="tabla"><thead><tr><th>Título</th><th>Tipo</th><th>Estado</th><th>Versión</th>
      <th class="ocultar-celu">Indexado</th><th class="ocultar-celu der">Consultas 30 d</th><th class="ocultar-celu">Fuente</th>
      <th class="ocultar-celu">Actualizado</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>""" \
        if trs else _vacio("No hay documentos con esos filtros.", "file-search")
    cuerpo = _cabecera("Documentos", "<span>SOPs, políticas, FAQs y manuales. Se dividen en fragmentos para la búsqueda de los agentes.</span>",
                       f'<a class="btn" href="/cargar">{ic("upload")}Cargar documento</a>')
    cuerpo += f'<section class="panel">{filtros}{tabla}</section>'
    return _pagina(request, ident, "Documentos", cuerpo, "documentos")


# ================================================================ elemento

@protegido
async def elemento(request: Request, ident: Identidad) -> Response:
    tipo, eid = request.path_params["tipo"], request.path_params["eid"]
    if tipo not in TIPOS:
        return Response(status_code=404)
    try:
        fila, versiones = c.elemento(ident.empresa_id, tipo, eid)
    except Exception:  # id mal formado
        fila, versiones = None, []
    seccion = "documentos" if tipo == "documento" else "conocimiento"
    if not fila:
        return _pagina(request, ident, "No existe", _cabecera("No existe") + _vacio("No existe ese elemento.", "circle-alert"), seccion)
    clave = c.TABLAS[tipo][1]
    vigente = next((v for v in versiones if v["estado"] == "aprobado"), None)

    aviso = ""
    if fila["estado"] != "aprobado":
        estado_txt = {"obsoleto": "obsoleta", "borrador": "en borrador"}[fila["estado"]]
        ir = (f' <a href="/k/{tipo}/{vigente["id"]}">Ver la vigente (v{vigente["version"]})</a>' if vigente
              else " No hay versión vigente: los agentes no lo ven.")
        aviso = f'<div class="alerta">{ic("history")}<p>Estás viendo la <b>v{fila["version"]}</b>, {estado_txt}.{ir}</p></div>'

    if tipo == "definicion":
        contenido = f'<div class="texto-largo">{e(fila["definicion"])}</div>'
        datos = [("Dueño", fila["dueno"]), ("Categoría", fila["categoria"])]
    elif tipo == "proceso":
        pasos = "".join(f"<li><span>{i:02d}</span><div>{e(_txt(x))}</div></li>" for i, x in enumerate(fila["pasos"] or [], 1))
        contenido = ((f'<div class="texto-largo">{e(fila["descripcion"])}</div>' if fila["descripcion"] else "") +
                     (f'<ol class="pasos-lista">{pasos}</ol>' if pasos else "") +
                     (f'<div class="nota" style="margin-bottom:14px"><b>Restricciones</b>{e(fila["restricciones"])}</div>'
                      if fila["restricciones"] else ""))
        datos = [("Responsable", fila["responsable"]), ("Pasos", str(len(fila["pasos"] or [])))]
    elif tipo == "regla":
        contenido = (f'<div class="texto-largo">{e(fila["restriccion"])}</div>' +
                     (f'<div class="nota" style="margin-bottom:14px"><b>Contexto</b>{e(fila["descripcion"])}</div>'
                      if fila["descripcion"] else ""))
        datos = [("Aplica a", fila["aplica_a"]), ("Obligatoria", "Sí, para esas áreas")]
    else:
        n_chunks = next((v["chunks"] for v in versiones if str(v["id"]) == str(fila["id"])), 0)
        contenido = f'<div class="texto-largo">{e(fila["contenido"])}</div>'
        datos = [("Tipo de doc.", fila["tipo"]), ("Fuente", fila["fuente"]), ("Fragmentos", str(n_chunks))]
    actual = next((v for v in versiones if str(v["id"]) == str(fila["id"])), {})
    filas_d = [("Estado", chip_estado(fila["estado"])), ("Tipo", _tipo(tipo)),
               ("Versión", f'<span class="mono">v{fila["version"]}</span> <span class="tenue">de {len(versiones)}</span>')]
    filas_d += [(k, e(v) if v else '<span class="tenue">—</span>') for k, v in datos]
    filas_d.append(("Aprobó", e(actual.get("revisado_por") or "—") if fila["estado"] != "borrador" else '<span class="tenue">Pendiente</span>'))
    filas_d.append(("Actualizado", f"{hace(fila['actualizado_en'])}<small>{fecha(fila['actualizado_en'])}</small>"))
    lado = _panel("Detalles", '<dl class="datos">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in filas_d) + "</dl>")

    trs = "".join(f"""<tr data-abrir="/k/{tipo}/{v['id']}" class="{'actual' if str(v['id']) == str(fila['id']) else ''}">
        <td class="num"><a class="nombre" href="/k/{tipo}/{v['id']}">v{v['version']}</a></td>
        <td>{chip_estado(v['estado'])}</td>
        <td title="{fecha(v.get('revisado_en') or v['creado_en'])}">{hace(v.get('revisado_en') or v['creado_en'])}</td>
        <td class="ocultar-celu">{e(v.get('propuesto_por') or '—')}</td>
        <td class="ocultar-celu">{e(v.get('revisado_por') or '—')}</td>
        <td class="texto"><small>{e(v.get('justificacion') or '')}</small></td>
        <td class="der">{f'<a class="enlace" href="/propuesta/{v["propuesta_id"]}">Propuesta {ic("arrow-up-right")}</a>' if v.get('propuesta_id') else ''}</td></tr>"""
                  for v in versiones)
    historial = _panel("Historial de versiones", f"""<div class="tabla-scroll"><table class="tabla"><thead><tr>
        <th>Versión</th><th>Estado</th><th>Fecha</th><th class="ocultar-celu">Propuso</th><th class="ocultar-celu">Aprobó</th>
        <th>Por qué</th><th></th></tr></thead><tbody>{trs}</tbody></table></div>""", cuenta=len(versiones),
                       sub="cada aprobación crea una versión; la anterior queda obsoleta, nunca se borra")
    sub = (f"{chip_estado(fila['estado'])}<span class='mono'>v{fila['version']}</span><span class=sep></span>"
           f"<span>actualizado {hace(fila['actualizado_en'])}</span>")
    migas = ([("/documentos", "Documentos")] if tipo == "documento"
             else [("/conocimiento", "Conocimiento"), (f"/conocimiento?tipo={tipo}", TIPOS[tipo][1])])
    cab = _cabecera(e(fila[clave]), sub, migas=migas)
    cuerpo = (cab + aviso + f'<div class="detalle"><div class="col">{_panel("Contenido", contenido)}{historial}</div>'
              f'<aside class="lado">{lado}</aside></div>')
    return _pagina(request, ident, fila[clave], cuerpo, seccion)


async def documento_viejo(request: Request) -> Response:  # enlaces de la versión anterior del panel
    return RedirectResponse(f"/k/documento/{request.path_params['did']}", status_code=301)


# ================================================================ actividad

@protegido
async def actividad(request: Request, ident: Identidad) -> Response:
    filtro = request.query_params.get("filtro", "todo")
    filtro = filtro if filtro in ("todo", "consultas", "cambios", "accesos") else "todo"
    actor = request.query_params.get("actor", "")
    eventos = c.actividad(ident.empresa_id, 300, filtro, actor)
    pest = _segm([(k, "/actividad?" + urlencode({"filtro": k, "actor": actor} if actor else {"filtro": k}), t, None)
                  for k, t in (("todo", "Todo"), ("consultas", "Consultas"), ("cambios", "Cambios"), ("accesos", "Accesos"))],
                 filtro)
    opciones = '<option value="">Todas las personas y agentes</option>' + "".join(
        f'<option{" selected" if a == actor else ""}>{e(a)}</option>' for a in c.actores(ident.empresa_id))
    filtros = f"""<form class="barra-filtros" method="get" action="/actividad">{pest}
      <input type="hidden" name="filtro" value="{filtro}">
      <select name="actor" data-auto aria-label="Quién">{opciones}</select>
      <span class="der">{plural(len(eventos), "evento", "eventos")}</span></form>"""
    cuerpo = _cabecera("Actividad", "<span>Registro de auditoría: todo lo que hicieron agentes y personas. No se puede editar ni borrar.</span>")
    cuerpo += f"""<section class="panel">{filtros}
      {f'<div class="log">{_log(eventos)}</div>' if eventos else _vacio("Sin actividad con estos filtros.", "info")}</section>"""
    return _pagina(request, ident, "Actividad", cuerpo, "actividad")


# ================================================================ búsqueda global

@protegido
async def buscar(request: Request, ident: Identidad) -> Response:
    q = request.query_params.get("q", "").strip()
    res = c.buscar(ident.empresa_id, q)
    total = sum(len(v) for v in res.values())

    def resaltar(texto: str) -> str:
        return e(texto or "").replace("⟦", "<mark>").replace("⟧", "</mark>")
    filas_k = "".join(f"""<a class="fila" href="/k/{k['tipo']}/{k['id']}"><span class="ic">{ic(TIPOS[k['tipo']][2])}</span>
        <div><h4>{e(k['nombre'])}</h4><p>{resaltar(k['resumen']) if k.get('resaltado') else e(k['resumen'] or '')}</p></div>
        <span class="der">{chip("gris", TIPOS[k['tipo']][0])}{chip_estado(k['estado'])}<span class="mono">v{k['version']}</span></span></a>"""
                      for k in res["conocimiento"])
    filas_p = "".join(f"""<a class="fila" href="/propuesta/{p['id']}"><span class="ic">{ic("git-pull-request-arrow")}</span>
        <div><h4>{e(nombre_propuesta(p))}</h4><p>{e(TIPOS[p['tipo']][0])} · propuso {e(p['propuesto_por'] or '—')}</p></div>
        <span class="der">{chip_estado(p['estado'])}{hace(p['creado_en'])}</span></a>"""
                      for p in res["propuestas"])
    bloques = ""
    if filas_k:
        bloques += _panel("Conocimiento", f'<div class="filas">{filas_k}</div>', cuenta=len(res["conocimiento"]))
    if filas_p:
        bloques += _panel("Propuestas", f'<div class="filas">{filas_p}</div>', cuenta=len(res["propuestas"]))
    if res["actividad"]:
        bloques += _panel("Actividad", f'<div class="log compacto">{_log(res["actividad"], agrupar=False)}</div>',
                          cuenta=len(res["actividad"]))
    if not q:
        sub = "<span>Un término, un proceso, una frase de un documento o el nombre de quien propuso.</span>"
    elif total:
        sub = f"<span>{plural(total, 'resultado', 'resultados')} para <b>“{e(q)}”</b>, incluyendo borradores y versiones viejas.</span>"
    else:
        sub = (f'<b class="mal">Nada para “{e(q)}”.</b><span>Si es algo que los agentes necesitan, '
               f'<a class="enlace" href="/cargar?titulo={quote(q)}">cargalo como documento</a>.</span>')
    caja = f"""<form class="buscar-grande" action="/buscar" role="search">{ic("search")}
      <input name="q" value="{e(q)}" placeholder="Buscar en definiciones, procesos, reglas, documentos, propuestas y actividad" autofocus></form>"""
    cuerpo = _cabecera("Búsqueda global", sub) + caja + f'<div class="pila">{bloques}</div>'
    return _pagina(request, ident, f"Buscar {q}", cuerpo, "buscar")


# ================================================================ cargar documento

@protegido
async def cargar(request: Request, ident: Identidad) -> Response:
    aviso = ""
    titulo = request.query_params.get("titulo", "")
    if request.method == "POST":
        form = await request.form()
        archivo = form.get("archivo")
        texto = str(form.get("contenido", ""))
        titulo = str(form.get("titulo", "")).strip()
        if archivo is not None and getattr(archivo, "filename", ""):
            datos = await archivo.read()
            if archivo.filename.lower().endswith(".pdf"):
                from pypdf import PdfReader
                texto = "\n\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(datos)).pages)
            else:
                texto = datos.decode("utf-8", errors="replace")
            titulo = titulo or archivo.filename.rsplit(".", 1)[0]
        try:
            r = g.cargar_documento(ident, titulo, texto, str(form.get("tipo") or "") or None,
                                   str(form.get("fuente") or "") or None,
                                   justificacion=str(form.get("justificacion") or "") or None)
            return RedirectResponse(f"/propuesta/{r['propuesta_id']}", status_code=303)
        except DatosInvalidos as ex:
            aviso = f'<div class="alerta roja">{ic("circle-alert")}<p>{e(str(ex))}</p></div>'
    formulario = f"""<form method="post" enctype="multipart/form-data" class="campos">
        {aviso}
        <label>Título<input name="titulo" value="{e(titulo)}" placeholder="Política de devoluciones">
          <small>Si ya hay un documento vigente con este título, al aprobarlo lo reemplaza (versión nueva).</small></label>
        <div class="dos">
          <label>Tipo<select name="tipo"><option>SOP</option><option>política</option><option>FAQ</option><option>manual</option><option>otro</option></select></label>
          <label>Fuente<input name="fuente" placeholder="Drive, persona, URL"></label>
        </div>
        <label>Archivo (.md, .txt o .pdf)<input type="file" name="archivo" accept=".md,.txt,.pdf"></label>
        <label>…o pegá el texto<textarea name="contenido" rows="12"></textarea></label>
        <label>Motivo<input name="justificacion" placeholder="Por qué se carga"></label>
        <div><button class="btn">{ic("upload")}Guardar en borrador</button></div>
      </form>"""
    ayuda = _panel("Qué pasa después", """<ol class="pasos-ayuda">
        <li><span>Se limpia el texto y se divide en <b>fragmentos</b> indexados para la búsqueda.</span></li>
        <li><span>Queda en <b>borrador</b> con una propuesta pendiente a tu nombre.</span></li>
        <li><span>Los agentes <b>no lo ven</b> hasta que alguien lo apruebe.</span></li>
        <li><span>Al aprobarlo pasa a <b>vigente</b>; si reemplaza a otro, el anterior queda obsoleto.</span></li></ol>""")
    cuerpo = _cabecera("Cargar documento", "<span>Entra como borrador y pasa por revisión antes de que lo lean los agentes.</span>",
                       migas=[("/documentos", "Documentos")])
    cuerpo += f'<div class="detalle"><div class="col">{_panel("Documento", formulario)}</div><aside class="lado">{ayuda}</aside></div>'
    return _pagina(request, ident, "Cargar", cuerpo, "cargar")


# ================================================================ app

@asynccontextmanager
async def _ciclo(_app):
    yield
    db.cerrar()


app = Starlette(routes=[
    Route("/", overview),
    Route("/entrar", entrar, methods=["GET", "POST"]),
    Route("/salir", salir),
    Route("/propuestas", propuestas),
    Route("/propuesta/{pid}", propuesta),
    Route("/propuestas/{pid}/{accion:str}", decidir, methods=["POST"]),
    Route("/conocimiento", conocimiento),
    Route("/documentos", documentos),
    Route("/k/{tipo}/{eid}", elemento),
    Route("/documento/{did}", documento_viejo),
    Route("/actividad", actividad),
    Route("/buscar", buscar),
    Route("/cargar", cargar, methods=["GET", "POST"]),
    Mount("/static", StaticFiles(directory=ESTATICOS), name="static"),
], lifespan=_ciclo)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or 4910))


if __name__ == "__main__":
    main()
