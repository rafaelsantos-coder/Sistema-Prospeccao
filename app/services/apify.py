from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import settings
from app.services.legacy_adapter import legacy_score
from app.services.scoring import calculate_sulnet_score, normalize_phone


class ApifyNotConfigured(RuntimeError):
    pass


class ApifySearchError(RuntimeError):
    pass


def _start_error(status_code: int) -> str:
    if status_code == 402:
        return (
            "Apify sem crédito ou limite do plano atingido (HTTP 402). "
            "Recarregue o saldo em console.apify.com > Billing e refaça a busca."
        )
    if status_code in {401, 403}:
        return f"Apify recusou o acesso (HTTP {status_code}). Verifique o token da integração."
    if status_code == 429:
        return "Apify limitou as requisições (HTTP 429). Tente novamente em alguns minutos."
    return f"Falha ao iniciar busca: HTTP {status_code}"


class ApifyService:
    base_url = "https://api.apify.com/v2"

    def __init__(self, token: str | None = None, actor_id: str | None = None) -> None:
        self.token = token or settings.apify_token
        self.actor_id = actor_id or settings.apify_actor_id

    async def search(self, *, segment: str, city: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.token:
            raise ApifyNotConfigured("A integração com o Apify ainda não foi configurada.")

        run_url = f"{self.base_url}/acts/{self.actor_id}/runs"
        payload = {
            "searchStringsArray": [f"{segment} em {city}"],
            "maxCrawledPlacesPerSearch": min(max(limit, 1), 100),
            "language": "pt-BR",
            "countryCode": "br",
        }
        params = {"token": self.token}

        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(run_url, params=params, json=payload)
            if response.status_code >= 400:
                raise ApifySearchError(_start_error(response.status_code))
            run_data = response.json().get("data", {})
            run_id = run_data.get("id")
            if not run_id:
                raise ApifySearchError("O Apify não retornou o identificador da busca.")

            dataset_id = await self._wait_for_run(client, run_id)
            items_response = await client.get(
                f"{self.base_url}/datasets/{dataset_id}/items",
                params={"token": self.token, "clean": "true", "limit": limit},
            )
            if items_response.status_code >= 400:
                raise ApifySearchError(f"Falha ao ler resultados: HTTP {items_response.status_code}")
            items = items_response.json()

        return [self._map_item(item, segment, city) for item in items]

    async def _wait_for_run(self, client: httpx.AsyncClient, run_id: str) -> str:
        for _ in range(60):
            response = await client.get(
                f"{self.base_url}/actor-runs/{run_id}", params={"token": self.token}
            )
            if response.status_code >= 400:
                raise ApifySearchError(f"Falha ao acompanhar busca: HTTP {response.status_code}")
            data = response.json().get("data", {})
            status = data.get("status")
            if status == "SUCCEEDED":
                dataset_id = data.get("defaultDatasetId")
                if not dataset_id:
                    raise ApifySearchError("Busca concluída sem dataset de resultados.")
                return dataset_id
            if status in {"FAILED", "ABORTED", "TIMED-OUT"}:
                raise ApifySearchError(f"Busca encerrada com status {status}.")
            await asyncio.sleep(5)
        raise ApifySearchError("A busca excedeu o tempo limite de cinco minutos.")

    @staticmethod
    def _map_item(item: dict[str, Any], segment: str, city: str) -> dict[str, Any]:
        phone = normalize_phone(item.get("phone") or item.get("phoneUnformatted"))
        mapped = {
            "business_name": item.get("title") or item.get("name") or "Empresa sem nome",
            "phone": phone,
            "email": item.get("email"),
            "website": item.get("website"),
            "segment": segment,
            "city": city,
            "address": item.get("address"),
            "reviews_count": item.get("reviewsCount") or 0,
            "rating": item.get("totalScore") or item.get("rating"),
            "source": "APIFY",
            "origin_channel": "WHATSAPP",
            "team": "B2B",
            "raw_data": item,
        }
        result = calculate_sulnet_score(mapped)
        mapped["score"] = result.score
        mapped["temperature"] = result.temperature
        mapped["potential"] = ", ".join(result.reasons)
        mapped["legacy_score"] = legacy_score(item)
        return mapped
