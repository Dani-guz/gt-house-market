from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScrapeResult:
    """Resultado de una corrida de una fuente: filas bronze y páginas que fallaron."""

    rows: list[dict] = field(default_factory=list)
    pages_ok: int = 0
    page_errors: list[str] = field(default_factory=list)


class ParseError(Exception):
    """La página no tiene la estructura esperada (probable cambio en el sitio)."""
