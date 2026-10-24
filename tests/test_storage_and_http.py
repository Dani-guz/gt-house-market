from __future__ import annotations

from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from scrapers.common.http import PoliteSession
from scrapers.common.storage import ENVELOPE_FIELDS, bronze_path, new_run_id, write_bronze

SCRAPED_AT = datetime(2026, 10, 24, 6, 0, 12, tzinfo=timezone.utc)


def test_bronze_path_is_hive_partitioned(tmp_path):
    path = bronze_path(tmp_path, "propilatam", SCRAPED_AT)
    assert path.relative_to(tmp_path).as_posix() == "source=propilatam/scrape_date=2026-10-24/run_060012Z.parquet"


def test_new_run_id_format():
    assert new_run_id(SCRAPED_AT) == "20261024T060012Z"


def test_write_bronze_roundtrip(tmp_path):
    schema = pa.schema(ENVELOPE_FIELDS + [pa.field("price", pa.float64())])
    rows = [
        {"run_id": "r1", "scraped_at": SCRAPED_AT, "source": "x", "source_page_url": "https://e.com", "price": 1.5},
    ]
    path = write_bronze(rows, schema, tmp_path, "x", SCRAPED_AT)

    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.schema.equals(schema)
    assert not list(tmp_path.rglob("*.tmp"))


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
