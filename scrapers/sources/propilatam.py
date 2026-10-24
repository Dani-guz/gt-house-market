"""Fuente: Propilatam (https://www.propilatam.com/gt).

Cómo vienen los datos
---------------------
El sitio es Next.js (App Router). El listado no hace llamadas a una API: el
servidor envía todos los proyectos dentro del "flight payload" de React Server
Components, en scripts ``self.__next_f.push([1, "<chunk>"])``. Este módulo:

1. Extrae y decodifica esos chunks (son literales JSON de string).
2. Busca la clave ``"initialProperties":`` y decodifica el arreglo JSON que sigue.
3. Mapea cada elemento a una fila bronze con tipos explícitos.

No se guardan galerías de fotos ni descripciones (son contenido con derechos de
autor del sitio). ``raw_json`` conserva el resto del objeto tal cual.

Estado observado (oct-2026): ~48 proyectos en venta (preventa y entrega
inmediata) y 0 en alquiler para Guatemala; no hay paginación.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import pyarrow as pa

from scrapers.common.http import BlockedError, PoliteSession, ScraperError
from scrapers.common.models import ParseError, ScrapeResult
from scrapers.common.storage import ENVELOPE_FIELDS

logger = logging.getLogger(__name__)

NAME = "propilatam"
BASE_URL = "https://www.propilatam.com"

# operation -> ruta del listado nacional (el filtro de Ciudad de Guatemala se hace en silver,
# porque el área metropolitana incluye municipios vecinos como Mixco o Santa Catarina Pinula).
LISTING_PAGES: dict[str, str] = {
    "for_sale": "/gt/bienes-raices/comprar/propiedades",
    "for_rent": "/gt/bienes-raices/alquilar/propiedades",
}
# Prefijo de la URL de detalle. "venta" está verificado; "renta" se infiere del sitemap.
_DETAIL_PREFIX = {"for_sale": "venta", "for_rent": "renta"}

# Claves que no se guardan ni en raw_json (imágenes con derechos de autor).
_EXCLUDED_KEYS = {"project_gallery"}

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.DOTALL)
_ANCHOR = '"initialProperties":'

SCHEMA = pa.schema(
    ENVELOPE_FIELDS
    + [
        pa.field("operation", pa.string()),  # for_sale | for_rent (marketplace_type)
        pa.field("listing_id", pa.string(), nullable=False),
        pa.field("listing_code", pa.string()),
        pa.field("listing_kind", pa.string()),  # project (desarrollo) | otros tipos futuros
        pa.field("listing_url", pa.string()),
        pa.field("title", pa.string()),
        pa.field("property_type", pa.string()),  # apto | house | ...
        pa.field("property_type_label", pa.string()),
        pa.field("address_text", pa.string()),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
        pa.field("currency_symbol", pa.string()),  # "Q" o "$" tal como lo muestra el sitio
        pa.field("price_min", pa.float64()),
        pa.field("price_max", pa.float64()),
        pa.field("price_text", pa.string()),
        pa.field("price_usd_text", pa.string()),
        pa.field("area_min_m2", pa.float64()),
        pa.field("area_max_m2", pa.float64()),
        pa.field("area_text", pa.string()),
        pa.field("land_area_varas_text", pa.string()),
        pa.field("bedrooms_min", pa.float64()),
        pa.field("bedrooms_max", pa.float64()),
        pa.field("bathrooms_min", pa.float64()),
        pa.field("bathrooms_max", pa.float64()),
        pa.field("parking_text", pa.string()),
        pa.field("available_units", pa.int64()),
        pa.field("work_progress", pa.string()),  # in_plans | in_construction | finalized
        pa.field("estimated_delivery", pa.string()),
        pa.field("developer_id", pa.string()),
        pa.field("developer_name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("raw_json", pa.string(), nullable=False),
    ]
)


# -- Parsing -------------------------------------------------------------------


def extract_flight_payload(html: str) -> str:
    """Concatena los chunks RSC ``self.__next_f.push([1, "..."])`` ya decodificados."""
    chunks = [json.loads(literal) for literal in _PUSH_RE.findall(html)]
    if not chunks:
        raise ParseError("No se encontraron chunks self.__next_f en la página")
    return "".join(chunks)


def extract_initial_properties(payload: str) -> list[dict[str, Any]]:
    """Decodifica el arreglo JSON que sigue a ``"initialProperties":``."""
    idx = payload.find(_ANCHOR)
    if idx == -1:
        raise ParseError(f"No se encontró {_ANCHOR} en el payload")
    try:
        value, _ = json.JSONDecoder().raw_decode(payload, idx + len(_ANCHOR))
    except json.JSONDecodeError as exc:
        raise ParseError(f"initialProperties no es JSON válido: {exc}") from exc
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError(f"initialProperties no es una lista (es {type(value).__name__})")
    return value


def parse_listing_page(html: str) -> list[dict[str, Any]]:
    """HTML de un listado -> lista de elementos crudos (con valores RSC decodificados)."""
    return [_decode_rsc(entry) for entry in extract_initial_properties(extract_flight_payload(html))]


def _decode_rsc(value: Any) -> Any:
    """Deshace las convenciones de serialización de RSC para strings.

    ``"$undefined"`` -> None, ``"$$100"`` -> ``"$100"`` (escape del signo $),
    ``"$D2026-..."`` -> ``"2026-..."`` (marcador de fecha).
    """
    if isinstance(value, dict):
        return {k: _decode_rsc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_rsc(v) for v in value]
    if isinstance(value, str):
        if value == "$undefined":
            return None
        if value.startswith("$$"):
            return value[1:]
        if value.startswith("$D"):
            return value[2:]
    return value


# -- Mapeo a bronze ---------------------------------------------------------------


def to_bronze_row(
    entry: dict[str, Any], *, operation: str, run_id: str, scraped_at: datetime, page_url: str
) -> dict[str, Any]:
    item = entry.get("item") or {}
    coords = entry.get("coordinates") or item.get("project_coordinates") or {}
    developer = item.get("developer") or {}
    listing_id = entry.get("id") or item.get("project_id")
    if not listing_id:
        raise ParseError("Elemento sin id")

    raw = {k: v for k, v in entry.items() if k != "item"}
    raw["item"] = {k: v for k, v in item.items() if k not in _EXCLUDED_KEYS}

    return {
        "run_id": run_id,
        "scraped_at": scraped_at,
        "source_page_url": page_url,
        "operation": item.get("marketplace_type") or operation,
        "listing_id": str(listing_id),
        "listing_code": _str(item.get("project_code")),
        "listing_kind": _str(entry.get("type")),
        "listing_url": _listing_url(item, operation),
        "title": _str(item.get("project_name")),
        "property_type": _str(item.get("project_type")),
        "property_type_label": _str(item.get("project_type_value")),
        "address_text": _str(item.get("project_address")),
        "lat": _float(coords.get("lat")),
        "lon": _float(coords.get("lng")),
        "currency_symbol": _str(item.get("min_price_currency_symbol")),
        "price_min": _float(item.get("price_value")),
        "price_max": _float(item.get("max_price_value")),
        "price_text": _str(item.get("min_price")),
        "price_usd_text": _str(item.get("exchange_min_price")),
        "area_min_m2": _float(item.get("min_area")),
        "area_max_m2": _float(item.get("max_area")),
        "area_text": _str(item.get("project_resume_area")),
        "land_area_varas_text": _str(item.get("project_resume_square_rods")),
        "bedrooms_min": _float(item.get("min_bedrooms")),
        "bedrooms_max": _float(item.get("max_bedrooms")),
        "bathrooms_min": _float(item.get("min_bathrooms")),
        "bathrooms_max": _float(item.get("max_bathrooms")),
        "parking_text": _str(item.get("project_resume_parking")),
        "available_units": _int(item.get("available_units")),
        "work_progress": _str(item.get("work_progress")),
        "estimated_delivery": _str(item.get("estimate_delivery_at")),
        "developer_id": _str(developer.get("id")),
        "developer_name": _str(developer.get("name")),
        "tags": [t["key"] for t in item.get("project_special_tags") or [] if isinstance(t, dict) and t.get("key")],
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


def _listing_url(item: dict[str, Any], operation: str) -> str | None:
    slug, code = item.get("url_detail"), item.get("project_code")
    if not slug or not code:
        return None
    return f"{BASE_URL}/gt/{_DETAIL_PREFIX.get(operation, 'venta')}/{slug}/{code}"


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "")) if isinstance(value, str) else float(value)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


# -- Orquestación -----------------------------------------------------------------


def scrape(session: PoliteSession, run_id: str, scraped_at: datetime) -> ScrapeResult:
    result = ScrapeResult()
    seen: set[str] = set()
    for operation, path in LISTING_PAGES.items():
        url = BASE_URL + path
        try:
            entries = parse_listing_page(session.get(url).text)
        except BlockedError:
            raise  # un bloqueo detiene toda la corrida
        except (ParseError, ScraperError) as exc:
            logger.error("Página %s falló: %s", url, exc)
            result.page_errors.append(f"{url}: {exc}")
            continue

        added = 0
        for entry in entries:
            try:
                row = to_bronze_row(entry, operation=operation, run_id=run_id, scraped_at=scraped_at, page_url=url)
            except ParseError as exc:
                logger.warning("Elemento descartado en %s: %s", url, exc)
                continue
            if row["listing_id"] in seen:
                continue
            seen.add(row["listing_id"])
            result.rows.append(row)
            added += 1
        result.pages_ok += 1
        logger.info("%s [%s]: %d elementos, %d filas nuevas", NAME, operation, len(entries), added)
    return result
