"""CLI del scraper: una corrida = un Parquet por fuente en data/bronze.

Uso:
    python -m scrapers.run --source propilatam
    python -m scrapers.run --source propilatam --dry-run --log-level DEBUG

Códigos de salida:
    0  corrida completa
    1  error inesperado o alguna página falló (lo que sí se obtuvo se escribe igual)
    2  el sitio bloqueó o robots.txt no permite la petición: se detuvo sin reintentar
    3  la corrida no produjo filas (no se escribe archivo)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from scrapers.common.http import BlockedError, DisallowedByRobotsError, HttpConfig, PoliteSession
from scrapers.common.storage import new_run_id, write_bronze
from scrapers.sources import SOURCES

logger = logging.getLogger("scrapers.run")

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_EMPTY = 0, 1, 2, 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, choices=sorted(SOURCES))
    parser.add_argument("--out", type=Path, default=Path("data/bronze"), help="Raíz de la capa bronze")
    parser.add_argument("--dry-run", action="store_true", help="Scrapea pero no escribe el Parquet")
    parser.add_argument("--min-delay", type=float, default=HttpConfig.min_delay)
    parser.add_argument("--max-delay", type=float, default=HttpConfig.max_delay)
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    source = SOURCES[args.source]
    scraped_at = datetime.now(timezone.utc)
    run_id = new_run_id(scraped_at)
    config = HttpConfig(min_delay=args.min_delay, max_delay=args.max_delay)
    logger.info("Inicio run_id=%s source=%s UA=%r", run_id, source.NAME, config.user_agent)

    try:
        with PoliteSession(config) as session:
            result = source.scrape(session, run_id=run_id, scraped_at=scraped_at)
            requests_made = session.requests_made
    except (BlockedError, DisallowedByRobotsError) as exc:
        logger.error("Corrida detenida: %s", exc)
        _write_summary(source.NAME, run_id, status="bloqueada", detail=str(exc))
        return EXIT_BLOCKED
    except Exception:
        logger.exception("Error inesperado")
        _write_summary(source.NAME, run_id, status="error")
        return EXIT_ERROR

    logger.info(
        "Fin scrape: %d filas, %d páginas OK, %d con error, %d peticiones",
        len(result.rows), result.pages_ok, len(result.page_errors), requests_made,
    )
    if not result.rows:
        _write_summary(source.NAME, run_id, status="sin filas", errors=result.page_errors)
        return EXIT_EMPTY

    path = None
    if args.dry_run:
        logger.info("--dry-run: no se escribe Parquet")
    else:
        path = write_bronze(result.rows, source.SCHEMA, args.out, source.NAME, scraped_at)

    status = "ok" if not result.page_errors else "parcial"
    _write_summary(source.NAME, run_id, status=status, rows=len(result.rows), path=path, errors=result.page_errors)
    return EXIT_OK if not result.page_errors else EXIT_ERROR


def _write_summary(source: str, run_id: str, *, status: str, rows: int = 0, path: Path | None = None,
                   detail: str = "", errors: list[str] | None = None) -> None:
    """Escribe un resumen en Markdown para la pestaña Summary de GitHub Actions."""
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    lines = [
        f"### Scrape `{source}` — {status}",
        "",
        "| run_id | filas | archivo |",
        "|---|---|---|",
        f"| `{run_id}` | {rows} | `{path.as_posix() if path else '-'}` |",
    ]
    if detail:
        lines += ["", f"> {detail}"]
    for err in errors or []:
        lines.append(f"- ⚠️ {err}")
    with open(summary_file, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
