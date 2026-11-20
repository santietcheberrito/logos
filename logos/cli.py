"""Comandos de administración.

  python -m logos.cli crear-empresa "Nombre"          crea empresa + token admin + token agente
  python -m logos.cli crear-token EMPRESA_ID "Nombre" agente|admin
  python -m logos.cli cargar ARCHIVO --token TOKEN [--tipo SOP] [--titulo ...]
  python -m logos.cli reindexar                        embeddings de chunks que no los tienen
  python -m logos.cli ejemplos --token TOKEN_ADMIN     carga y aprueba los datos de ejemplo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import chunking, db, gobernanza

EJEMPLOS = Path(__file__).resolve().parent.parent / "ejemplos"


def crear_empresa(nombre: str) -> None:
    with db.conexion() as conn:
        fila = conn.execute("insert into empresas (nombre) values (%s) returning id", (nombre,)).fetchone()
    empresa_id = str(fila["id"])
    admin = gobernanza.crear_token(empresa_id, f"Admin de {nombre}", "admin")
    agente = gobernanza.crear_token(empresa_id, "Agente general", "agente")
    print(f"Empresa: {nombre}\nempresa_id: {empresa_id}\n")
    print("Guardá estos tokens ahora: no se vuelven a mostrar.\n")
    print(f"TOKEN ADMIN : {admin}")
    print(f"TOKEN AGENTE: {agente}")


def cargar(archivo: str, token: str, tipo: str | None, titulo: str | None, fuente: str | None) -> None:
    ident = gobernanza.identificar(token)
    ruta = Path(archivo)
    texto = chunking.leer_archivo(ruta)
    r = gobernanza.cargar_documento(ident, titulo or ruta.stem.replace("_", " ").replace("-", " "),
                                    texto, tipo, fuente or ruta.name)
    print(json.dumps(r, ensure_ascii=False, indent=2))


def ejemplos(token: str) -> None:
    ident = gobernanza.identificar(token)
    gobernanza.exigir_admin(ident)
    datos = json.loads((EJEMPLOS / "conocimiento.json").read_text(encoding="utf-8"))
    total = 0
    for tipo in ("definicion", "proceso", "regla"):
        for item in datos[tipo]:
            p = gobernanza.proponer(ident, tipo, item, "Carga inicial de ejemplo")
            gobernanza.aprobar(ident, p["propuesta_id"])
            total += 1
    for doc in sorted((EJEMPLOS / "documentos").glob("*.md")):
        texto = doc.read_text(encoding="utf-8")
        titulo = texto.splitlines()[0].lstrip("# ").strip()
        r = gobernanza.cargar_documento(ident, titulo, texto, "política" if "politica" in doc.name else "SOP",
                                        f"ejemplos/{doc.name}")
        gobernanza.aprobar(ident, r["propuesta_id"])
        total += 1
    print(f"Cargados y aprobados {total} elementos de ejemplo.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="logos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("crear-empresa"); s.add_argument("nombre")
    s = sub.add_parser("crear-token"); s.add_argument("empresa_id"); s.add_argument("nombre")
    s.add_argument("rol", choices=["agente", "admin"])
    s = sub.add_parser("cargar"); s.add_argument("archivo"); s.add_argument("--token", required=True)
    s.add_argument("--tipo"); s.add_argument("--titulo"); s.add_argument("--fuente")
    sub.add_parser("reindexar")
    s = sub.add_parser("ejemplos"); s.add_argument("--token", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "crear-empresa":
            crear_empresa(a.nombre)
        elif a.cmd == "crear-token":
            print(gobernanza.crear_token(a.empresa_id, a.nombre, a.rol))
        elif a.cmd == "cargar":
            cargar(a.archivo, a.token, a.tipo, a.titulo, a.fuente)
        elif a.cmd == "reindexar":
            print(f"Chunks reindexados: {gobernanza.reindexar()}")
        elif a.cmd == "ejemplos":
            ejemplos(a.token)
    except (gobernanza.AccesoDenegado, gobernanza.DatosInvalidos) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.cerrar()


if __name__ == "__main__":
    main()
