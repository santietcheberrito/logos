"""Configuración por variables de entorno (.env en la raíz del proyecto)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env", override=False)


def _env(nombre: str, defecto: str | None = None) -> str | None:
    valor = os.environ.get(nombre)
    return valor if valor not in (None, "") else defecto


# Conexión de BACKEND a Postgres (Supabase: "Connection string" del proyecto,
# rol postgres). Salta RLS: nunca se entrega a un agente ni al navegador.
DATABASE_URL = _env("DATABASE_URL", "postgresql://logos@127.0.0.1:55440/logos")

# Embeddings: "openai" (producción) o "local" (solo pruebas, sin red, calidad baja).
EMBEDDINGS_PROVIDER = _env("EMBEDDINGS_PROVIDER", "openai")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
EMBEDDINGS_MODEL = _env("EMBEDDINGS_MODEL", "text-embedding-3-small")
EMBEDDINGS_DIM = 1536

# Chunking (tokens)
CHUNK_TOKENS = int(_env("CHUNK_TOKENS", "700"))
CHUNK_OVERLAP = int(_env("CHUNK_OVERLAP", "100"))

# Token del agente cuando el MCP corre por stdio (Claude Desktop / Cursor)
LOGOS_TOKEN = _env("LOGOS_TOKEN")

# Dashboard
DASHBOARD_SECRET = _env("DASHBOARD_SECRET")
