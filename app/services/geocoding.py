"""Geocodificação de bairros pelo Nominatim (OpenStreetMap).

Só nomes de bairro/cidade saem do sistema, nunca dado pessoal. Respeita a política
do serviço público: identificação do app, no máximo 1 consulta por segundo e
resultado guardado no banco (ver ``CreditMapGeocode``).
"""

from __future__ import annotations

import math
import time
from typing import Any

import httpx

from app.config import settings

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_INTERVAL_SECONDS = 1.1
MAX_DISTANCE_KM = 32.0
_last_call = 0.0


class GeocodingError(RuntimeError):
    pass


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6371.0
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))


def user_agent() -> str:
    """O Nominatim exige identificar o aplicativo; usamos o endereço público do sistema."""
    site = (settings.app_public_url or "").strip()
    return f"SulnetProspecta/1.0 (+{site})" if site else "SulnetProspecta/1.0"


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def search(query: str) -> tuple[float, float, str] | None:
    """Primeiro resultado no Brasil, ou None se o Nominatim não achar."""
    _throttle()
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": user_agent(), "Accept-Language": "pt-BR"}) as client:
            response = client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "br"},
            )
    except httpx.HTTPError as exc:
        raise GeocodingError("Serviço de endereços indisponível.") from exc
    if response.status_code in (429, 503):
        raise GeocodingError("Serviço de endereços ocupado; tente de novo em instantes.")
    if response.status_code >= 400:
        raise GeocodingError(f"Serviço de endereços respondeu {response.status_code}.")
    try:
        rows: list[dict[str, Any]] = response.json()
        first = rows[0]
        return float(first["lat"]), float(first["lon"]), str(first.get("display_name") or "")
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def city_center(city: str) -> tuple[float, float] | None:
    found = search(f"{city}, Rio Grande do Sul, Brasil")
    return (found[0], found[1]) if found else None


def neighborhood_center(name: str, city: str, center: tuple[float, float]) -> tuple[float, float] | None:
    """Centro do bairro, descartando resultado longe da cidade (homônimos de outras cidades)."""
    found = search(f"{name}, {city}, Rio Grande do Sul, Brasil")
    if not found:
        return None
    point = (found[0], found[1])
    return point if distance_km(point, center) <= MAX_DISTANCE_KM else None


def street_point(
    street: str, neighborhood: str, city: str, cep: str, center: tuple[float, float],
) -> tuple[float, float] | None:
    """Posição aproximada da rua (meio do logradouro), descartando resultado longe da cidade."""
    queries = [f"{street}, {neighborhood}, {city}, Rio Grande do Sul, Brasil"]
    queries.append(f"{street}, {city}, Rio Grande do Sul, Brasil")
    if cep:
        queries.append(f"{cep[:5]}-{cep[5:]}, {city}, Rio Grande do Sul, Brasil")
    for query in queries:
        found = search(query)
        if not found:
            continue
        point = (found[0], found[1])
        if distance_km(point, center) <= MAX_DISTANCE_KM:
            return point
    return None
