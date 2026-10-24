"""Escritura de la capa bronze en Parquet.

Layout (estilo Hive, lo lee DuckDB/dbt con ``hive_partitioning``):

    data/bronze/source=<fuente>/scrape_date=YYYY-MM-DD/run_<HHMMSS>Z.parquet

Un archivo por corrida y fuente. El esquema lo define cada fuente con pyarrow,
así un cambio en el sitio rompe la escritura en vez de colarse en silencio.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Columnas de control que toda fuente lleva en bronze.
ENVELOPE_FIELDS = [
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("scraped_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("source_page_url", pa.string(), nullable=False),
]


def new_run_id(now: datetime | None = None) -> str:
    """Identificador de corrida ordenable: ``20261024T060012Z``."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def bronze_path(base_dir: Path, source: str, scraped_at: datetime) -> Path:
    scraped_at = scraped_at.astimezone(timezone.utc)
    return (
        base_dir
        / f"source={source}"
        / f"scrape_date={scraped_at:%Y-%m-%d}"
        / f"run_{scraped_at:%H%M%S}Z.parquet"
    )


def write_bronze(
    records: Iterable[Mapping[str, object]],
    schema: pa.Schema,
    base_dir: Path,
    source: str,
    scraped_at: datetime,
) -> Path:
    """Valida ``records`` contra ``schema`` y escribe un Parquet comprimido con zstd."""
    rows = list(records)
    table = pa.Table.from_pylist(rows, schema=schema)
    path = bronze_path(base_dir, source, scraped_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)  # escritura atómica: nunca queda un Parquet a medias
    logger.info("Escritas %d filas en %s (%.1f KB)", table.num_rows, path, path.stat().st_size / 1024)
    return path
