"""Registro de fuentes. Cada fuente es un módulo que expone:

- ``NAME``: identificador usado en la partición ``source=<NAME>``.
- ``SCHEMA``: ``pyarrow.Schema`` de su tabla bronze.
- ``scrape(session, run_id, scraped_at) -> ScrapeResult``.

Agregar una fuente nueva = crear el módulo y registrarlo aquí.
"""

from __future__ import annotations

from types import ModuleType

from scrapers.sources import propilatam

SOURCES: dict[str, ModuleType] = {
    propilatam.NAME: propilatam,
}
