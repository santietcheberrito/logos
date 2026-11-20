"""Generación de embeddings.

- "openai": text-embedding-3-small (1536 dimensiones). Es el modo de producción.
- "local": vector por hashing de palabras, sin red. SOLO para pruebas: sirve para
  que el pipeline funcione sin clave, pero no entiende sinónimos.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

import httpx

from . import config


class EmbeddingsNoDisponibles(RuntimeError):
    pass


def proveedor() -> str:
    return config.EMBEDDINGS_PROVIDER or "openai"


def disponible() -> bool:
    if proveedor() == "local":
        return True
    return bool(config.OPENAI_API_KEY)


def generar(textos: list[str]) -> list[list[float]]:
    if not textos:
        return []
    if proveedor() == "local":
        return [_local(t) for t in textos]
    if proveedor() != "openai":
        raise EmbeddingsNoDisponibles(f"Proveedor de embeddings desconocido: {proveedor()}")
    if not config.OPENAI_API_KEY:
        raise EmbeddingsNoDisponibles("Falta OPENAI_API_KEY en .env")
    vectores: list[list[float]] = []
    with httpx.Client(timeout=60) as cliente:
        for i in range(0, len(textos), 96):
            lote = textos[i : i + 96]
            r = cliente.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                json={"model": config.EMBEDDINGS_MODEL, "input": lote},
            )
            if r.status_code != 200:
                raise EmbeddingsNoDisponibles(f"OpenAI respondió {r.status_code}: {r.text[:300]}")
            datos = sorted(r.json()["data"], key=lambda d: d["index"])
            vectores.extend(d["embedding"] for d in datos)
    return vectores


def _normalizar(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9ñ]{3,}", texto)


def _local(texto: str) -> list[float]:
    vec = [0.0] * config.EMBEDDINGS_DIM
    for palabra in _normalizar(texto):
        # raíz aproximada: primeras 6 letras, para que "facturas" ~ "facturación"
        for token in {palabra, palabra[:6]}:
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % config.EMBEDDINGS_DIM] += 1.0 if (h >> 20) & 1 else -1.0
    norma = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norma for v in vec]


def a_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
