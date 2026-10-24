"""Tests del parser de Propilatam con un payload RSC sintético (basado en un elemento real, sin fotos)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pyarrow as pa
import pytest

from scrapers.common.models import ParseError
from scrapers.sources import propilatam

ENTRY = {
    "id": "bb227027-de01-467a-8508-d78854212cd5",
    "type": "project",
    "coordinates": {"lat": 14.543385787, "lng": -90.454156443},
    "distance": None,
    "item": {
        "project_id": "bb227027-de01-467a-8508-d78854212cd5",
        "marketplace_type": "for_sale",
        "project_code": "1018",
        "project_name": "Monet",
        "project_address": "Carretera a Laguna Bermeja, Lote 118, Zona 6, Santa Catarina Pinula, Guatemala",
        "project_gallery": ["https://example.com/foto.jpg"],
        "project_type": "apto",
        "project_type_value": "Apartamento",
        "project_resume_area": "De 74 a 85 ",
        "project_resume_parking": "De 2 Parqueos",
        "project_resume_square_rods": None,
        "max_price_value": None,
        "exchange_min_price": "$$100,032",
        "price_value": 787252,
        "min_price": "Q787,252",
        "min_price_currency_symbol": "Q",
        "min_area": 74,
        "max_area": 85,
        "min_bathrooms": 2,
        "max_bathrooms": 2,
        "min_bedrooms": 3,
        "max_bedrooms": 3,
        "available_units": 26,
        "url_detail": "monet",
        "estimate_delivery_at": "Q2 2025",
        "work_progress": "finalized",
        "developer": {"id": "2985034a-1293-4c97-b7b0-9d346b2e7368", "name": "Asty"},
        "project_special_tags": [{"icon": "IconChartLine", "name": "Proyectos de inversión", "key": "invest-projects"}],
        "indicators": "$undefined",
    },
}

SCRAPED_AT = datetime(2026, 10, 24, 6, 0, 12, tzinfo=timezone.utc)
PAGE_URL = "https://www.propilatam.com/gt/bienes-raices/comprar/propiedades"


def make_html(properties: object, split_at: int | None = None) -> str:
    """Simula cómo Next.js incrusta el payload: uno o varios chunks push([1, "<json string>"])."""
    payload = '9:["$","div",null,{"initialFilter":{"marketplace_type":"for_sale"},' + (
        f'"initialProperties":{json.dumps(properties, ensure_ascii=False)},"suggestedZones":"$1a"}}]\n'
    )
    parts = [payload] if split_at is None else [payload[:split_at], payload[split_at:]]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(p)}])</script>" for p in parts)
    return f'<html><body><script>self.__next_f.push([0])</script>{scripts}</body></html>'


def test_parse_listing_page_extracts_entries():
    entries = propilatam.parse_listing_page(make_html([ENTRY]))
    assert len(entries) == 1
    assert entries[0]["item"]["project_name"] == "Monet"


def test_payload_split_across_chunks_is_reassembled():
    entries = propilatam.parse_listing_page(make_html([ENTRY, ENTRY], split_at=120))
    assert len(entries) == 2


def test_rsc_values_are_decoded():
    item = propilatam.parse_listing_page(make_html([ENTRY]))[0]["item"]
    assert item["exchange_min_price"] == "$100,032"
    assert item["indicators"] is None


def test_empty_listing_returns_no_entries():
    assert propilatam.parse_listing_page(make_html([])) == []


def test_missing_anchor_raises_parse_error():
    html = '<script>self.__next_f.push([1,"0:{\\"other\\":1}\\n"])</script>'
    with pytest.raises(ParseError):
        propilatam.parse_listing_page(html)


def test_page_without_flight_payload_raises_parse_error():
    with pytest.raises(ParseError):
        propilatam.parse_listing_page("<html><body>Hola</body></html>")


def test_to_bronze_row_maps_fields_and_drops_gallery():
    entry = propilatam.parse_listing_page(make_html([ENTRY]))[0]
    row = propilatam.to_bronze_row(entry, operation="for_sale", run_id="r1", scraped_at=SCRAPED_AT, page_url=PAGE_URL)

    assert row["listing_id"] == ENTRY["id"]
    assert row["operation"] == "for_sale"
    assert row["price_min"] == 787252.0
    assert row["price_max"] is None
    assert row["currency_symbol"] == "Q"
    assert row["area_min_m2"] == 74.0 and row["area_max_m2"] == 85.0
    assert row["lat"] == pytest.approx(14.543385787)
    assert row["lon"] == pytest.approx(-90.454156443)
    assert row["listing_url"] == "https://www.propilatam.com/gt/venta/monet/1018"
    assert row["developer_name"] == "Asty"
    assert row["tags"] == ["invest-projects"]

    raw = json.loads(row["raw_json"])
    assert "project_gallery" not in raw["item"]
    assert raw["item"]["project_name"] == "Monet"


def test_rows_conform_to_schema():
    entry = propilatam.parse_listing_page(make_html([ENTRY]))[0]
    row = propilatam.to_bronze_row(entry, operation="for_sale", run_id="r1", scraped_at=SCRAPED_AT, page_url=PAGE_URL)
    table = pa.Table.from_pylist([row], schema=propilatam.SCHEMA)
    assert table.num_rows == 1
    assert set(row) == set(propilatam.SCHEMA.names)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeSession:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(self, url: str) -> FakeResponse:
        return FakeResponse(self.pages[url])


def test_scrape_dedupes_and_reports_bad_pages():
    sale_url = propilatam.BASE_URL + propilatam.LISTING_PAGES["for_sale"]
    rent_url = propilatam.BASE_URL + propilatam.LISTING_PAGES["for_rent"]
    session = FakeSession({sale_url: make_html([ENTRY, ENTRY]), rent_url: "<html>roto</html>"})

    result = propilatam.scrape(session, run_id="r1", scraped_at=SCRAPED_AT)

    assert len(result.rows) == 1
    assert result.pages_ok == 1
    assert len(result.page_errors) == 1 and rent_url in result.page_errors[0]
