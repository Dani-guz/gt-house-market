from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
import requests

from scrapers.common.http import PoliteSession
from scrapers.common.storage import ENVELOPE_FIELDS, bronze_path, new_run_id, write_bronze

SCRAPED_AT = datetime(2026, 10, 24, 6, 0, 12, tzinfo=timezone.utc)


def test_bronze_path_is_hive_partitioned(tmp_path):
    path = bronze_path(tmp_path, "propilatam", SCRAPED_AT)
    assert path.relative_to(tmp_path).as_posix() == "source=propilatam/scrape_date=2026-10-24/run_060012Z.parquet"


def test_new_run_id_format():
    assert new_run_id(SCRAPED_AT) == "20261024T060012Z"


SCHEMA = pa.schema(ENVELOPE_FIELDS + [pa.field("price", pa.float64())])
ROWS = [{"run_id": "r1", "scraped_at": SCRAPED_AT, "source_page_url": "https://e.com", "price": 1.5}]


def test_write_bronze_roundtrip(tmp_path):
    path = write_bronze(ROWS, SCHEMA, tmp_path, "x", SCRAPED_AT)

    assert pq.read_schema(path).equals(SCHEMA)
    assert pq.ParquetFile(path).metadata.num_rows == 1
    assert not list(tmp_path.rglob("*.tmp"))


def test_bronze_dataset_exposes_partition_columns(tmp_path):
    write_bronze(ROWS, SCHEMA, tmp_path, "x", SCRAPED_AT)

    table = ds.dataset(tmp_path, format="parquet", partitioning="hive").to_table()
    assert table.num_rows == 1
    assert table.column("source").to_pylist() == ["x"]
    assert str(table.column("scrape_date").to_pylist()[0]).startswith("2026-10-24")


def test_schema_with_partition_column_is_rejected(tmp_path):
    bad = SCHEMA.append(pa.field("source", pa.string()))
    with pytest.raises(ValueError, match="partición"):
        write_bronze(ROWS, bad, tmp_path, "x", SCRAPED_AT)


def _response(status: int, body: str) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    resp.encoding = "utf-8"
    return resp


def test_block_detection():
    assert PoliteSession._is_blocked(_response(403, "Forbidden"))
    assert PoliteSession._is_blocked(_response(429, "Too Many Requests"))
    assert PoliteSession._is_blocked(_response(503, "<title>Just a moment...</title>"))
    assert PoliteSession._is_blocked(_response(200, "<script src='https://challenges.cloudflare.com/x'>"))
    assert not PoliteSession._is_blocked(_response(200, "<html>listado normal</html>"))
    assert not PoliteSession._is_blocked(_response(503, "Service Unavailable"))
