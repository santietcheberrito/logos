"""Limpieza y división de texto en chunks por tokens (con solapamiento)."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from . import config

try:  # tiktoken baja su tabla la primera vez; sin red caemos a una aproximación
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover
    _ENC = None


def limpiar(texto: str) -> str:
    texto = unicodedata.normalize("NFC", texto)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    texto = "".join(c for c in texto if c == "\n" or unicodedata.category(c)[0] != "C")
    texto = re.sub(r"[  ]+", " ", texto)
    texto = re.sub(r" *\n *", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def contar_tokens(texto: str) -> int:
    if _ENC is not None:
        return len(_ENC.encode(texto))
    return int(len(texto.split()) * 1.4)


def dividir(texto: str, tokens: int | None = None, solapamiento: int | None = None) -> list[str]:
    """Ventana deslizante de `tokens` con `solapamiento`, respetando párrafos cuando se puede."""
    tokens = tokens or config.CHUNK_TOKENS
    solapamiento = solapamiento if solapamiento is not None else config.CHUNK_OVERLAP
    if solapamiento >= tokens:
        raise ValueError("El solapamiento debe ser menor que el tamaño del chunk")
    texto = limpiar(texto)
    if not texto:
        return []

    parrafos = [p for p in texto.split("\n\n") if p.strip()]
    chunks: list[str] = []
    actual: list[str] = []
    actual_tokens = 0
    hay_nuevo = False  # actual tiene algo más que el solapamiento arrastrado

    def cerrar() -> None:
        nonlocal actual, actual_tokens, hay_nuevo
        if not actual or not hay_nuevo:
            return
        hay_nuevo = False
        chunks.append("\n\n".join(actual))
        # arrastrar el final como solapamiento
        cola: list[str] = []
        cola_tokens = 0
        for p in reversed(actual):
            t = contar_tokens(p)
            if cola_tokens + t > solapamiento:
                break
            cola.insert(0, p)
            cola_tokens += t
        if not cola and solapamiento > 0:
            ultimo = _ultimos_tokens(actual[-1], solapamiento)
            cola, cola_tokens = [ultimo], contar_tokens(ultimo)
        actual, actual_tokens = cola, cola_tokens

    for p in parrafos:
        t = contar_tokens(p)
        if t > tokens:  # párrafo enorme: partirlo por tokens
            cerrar()
            for pedazo in _partir_por_tokens(p, tokens, solapamiento):
                chunks.append(pedazo)
            actual, actual_tokens = [], 0
            continue
        if actual_tokens + t > tokens:
            cerrar()
        actual.append(p)
        actual_tokens += t
        hay_nuevo = True
    if actual and hay_nuevo:
        chunks.append("\n\n".join(actual))
    return chunks


def _partir_por_tokens(texto: str, tokens: int, solapamiento: int) -> list[str]:
    if _ENC is None:
        palabras = texto.split()
        paso_p = max(1, int((tokens - solapamiento) / 1.4))
        largo_p = int(tokens / 1.4)
        return [" ".join(palabras[i : i + largo_p]) for i in range(0, len(palabras), paso_p)]
    ids = _ENC.encode(texto)
    paso = tokens - solapamiento
    salida = []
    for i in range(0, len(ids), paso):
        salida.append(_ENC.decode(ids[i : i + tokens]))
        if i + tokens >= len(ids):
            break
    return salida


def _ultimos_tokens(texto: str, n: int) -> str:
    if _ENC is None:
        return " ".join(texto.split()[-int(n / 1.4) :])
    return _ENC.decode(_ENC.encode(texto)[-n:])


def leer_archivo(ruta: str | Path) -> str:
    """Lee .txt, .md o .pdf y devuelve texto plano."""
    ruta = Path(ruta)
    if ruta.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join((p.extract_text() or "") for p in PdfReader(str(ruta)).pages)
    return ruta.read_text(encoding="utf-8")
