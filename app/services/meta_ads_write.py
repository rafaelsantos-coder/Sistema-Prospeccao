"""Gestor de Tráfego Pago: criação de campanha na Meta, SEMPRE pausada.

Regras de segurança (valem para todo o módulo):
- só escreve na conta configurada em META_ADS_ACCOUNT_ID (a conta da BM03);
- tudo é criado com status PAUSED; este módulo não tem função de ativar;
- o token nunca aparece em mensagens de erro.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import httpx

from app.services.meta_ads import (
    GRAPH_BASE, MetaAdsError, MetaAdsSettings, _ACCOUNT_RE, _VERSION_RE, _get,
)

PAUSED = "PAUSED"


@dataclass
class AdSpec:
    name: str
    kind: str  # "image" | "video"
    headline: str
    text: str
    link_description: str
    utm_content: str


@dataclass
class CampaignPlan:
    name: str
    daily_budget_brl: float
    city_name: str
    region_name: str
    phone: str  # só dígitos, com DDI: 555535118003
    welcome_text: str
    utm_campaign: str
    ads: list[AdSpec] = field(default_factory=list)
    age_min: int = 18
    age_max: int = 65


@dataclass
class CreatedCampaign:
    campaign_id: str = ""
    adset_id: str = ""
    ad_ids: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


def _post(client: httpx.Client, settings: MetaAdsSettings, path: str, data: dict[str, Any] | None = None,
          files: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
    encoded = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in (data or {}).items()}
    try:
        response = client.post(
            f"{GRAPH_BASE}/{settings.version}/{path}", data=encoded, files=files,
            headers={"Authorization": f"Bearer {settings.token}"}, timeout=timeout,
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MetaAdsError("Não foi possível falar com a Meta ao enviar.") from exc
    error = body.get("error") if isinstance(body, dict) else None
    if response.status_code >= 300 or error or not isinstance(body, dict):
        err = error if isinstance(error, dict) else {}
        detail = err.get("error_user_msg") or err.get("message") or "sem detalhe"
        raise MetaAdsError("A Meta recusou (%s/%s): %s" % (err.get("code", "?"), err.get("error_subcode", "-"), str(detail)[:300]))
    return body


def _check_settings(settings: MetaAdsSettings) -> None:
    if not settings.token or not _ACCOUNT_RE.match(settings.account_id) or not _VERSION_RE.match(settings.version):
        raise MetaAdsError("Conexão com a Meta não configurada.")


def find_city_key(client: httpx.Client, settings: MetaAdsSettings, city: str, region: str) -> str:
    data = _get(client, settings, "search", {
        "type": "adgeolocation", "q": city, "location_types": json.dumps(["city"]), "country_code": "BR", "limit": 10,
    })
    for row in data.get("data", []):
        if isinstance(row, dict) and row.get("name", "").lower() == city.lower() and region.lower() in str(row.get("region", "")).lower():
            return str(row["key"])
    raise MetaAdsError("Cidade não encontrada na Meta: %s/%s." % (city, region))


def find_page_id(client: httpx.Client, settings: MetaAdsSettings) -> str:
    data = _get(client, settings, f"{settings.account_id}/promote_pages", {"fields": "id,name", "limit": 5})
    rows = [r for r in data.get("data", []) if isinstance(r, dict) and r.get("id")]
    if not rows:
        raise MetaAdsError("Nenhuma Página liberada para esta conta de anúncios.")
    return str(rows[0]["id"])


def upload_image(client: httpx.Client, settings: MetaAdsSettings, filename: str, content: bytes) -> str:
    body = _post(client, settings, f"{settings.account_id}/adimages", files={"filename": (filename, content)})
    images = body.get("images") or {}
    first = next(iter(images.values()), {}) if isinstance(images, dict) else {}
    if not first.get("hash"):
        raise MetaAdsError("A Meta não devolveu o código da imagem.")
    return str(first["hash"])


def upload_video(client: httpx.Client, settings: MetaAdsSettings, filename: str, fileobj: BinaryIO,
                 *, wait_seconds: int = 240) -> tuple[str, str]:
    """Envia o vídeo e espera a Meta processar. Devolve (video_id, url_da_miniatura)."""
    body = _post(client, settings, f"{settings.account_id}/advideos", data={"name": filename},
                 files={"source": (filename, fileobj)}, timeout=300)
    video_id = str(body.get("id") or "")
    if not video_id:
        raise MetaAdsError("A Meta não devolveu o código do vídeo.")
    deadline = time.monotonic() + wait_seconds
    while True:
        info = _get(client, settings, video_id, {"fields": "status,picture"})
        state = ((info.get("status") or {}).get("video_status") or "").lower()
        if state == "ready":
            return video_id, str(info.get("picture") or "")
        if state == "error":
            raise MetaAdsError("A Meta não conseguiu processar o vídeo.")
        if time.monotonic() > deadline:
            raise MetaAdsError("O vídeo ainda está sendo processado pela Meta. Tente de novo em alguns minutos.")
        time.sleep(5)


def create_paused_campaign(
    plan: CampaignPlan, *, image: tuple[str, bytes] | None, video: tuple[str, BinaryIO] | None,
    settings: MetaAdsSettings | None = None, client: httpx.Client | None = None,
) -> CreatedCampaign:
    """Cria campanha + conjunto + anúncios, tudo PAUSADO. Se falhar no meio, o erro informa o que já foi criado."""
    settings = settings or MetaAdsSettings.from_env()
    _check_settings(settings)
    if not plan.ads:
        raise MetaAdsError("Nenhum anúncio informado.")
    if not 5 <= plan.daily_budget_brl <= 1000:
        raise MetaAdsError("Orçamento diário fora do limite de segurança (R$ 5 a R$ 1.000).")
    if not plan.phone.isdigit():
        raise MetaAdsError("Telefone do WhatsApp inválido (use só números, com 55 na frente).")
    kinds = {a.kind for a in plan.ads}
    if "image" in kinds and not image:
        raise MetaAdsError("Falta o arquivo de imagem.")
    if "video" in kinds and not video:
        raise MetaAdsError("Falta o arquivo de vídeo.")

    owns_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=False)
    out = CreatedCampaign()
    acct = settings.account_id
    try:
        try:
            page_id = find_page_id(client, settings)
            city_key = find_city_key(client, settings, plan.city_name, plan.region_name)
            out.steps.append("Página e cidade conferidas")

            image_hash = ""
            if "image" in kinds and image:
                image_hash = upload_image(client, settings, image[0], image[1])
                out.steps.append("Imagem enviada")
            video_id, thumb = "", ""
            if "video" in kinds and video:
                video_id, thumb = upload_video(client, settings, video[0], video[1])
                out.steps.append("Vídeo enviado e processado")

            camp = _post(client, settings, f"{acct}/campaigns", {
                "name": plan.name, "objective": "OUTCOME_ENGAGEMENT", "status": PAUSED,
                "special_ad_categories": [], "is_adset_budget_sharing_enabled": False,
            })
            out.campaign_id = str(camp["id"])
            out.steps.append("Campanha criada (pausada)")

            adset = _post(client, settings, f"{acct}/adsets", {
                "name": f"{plan.city_name} - Advantage+ - WhatsApp", "campaign_id": out.campaign_id, "status": PAUSED,
                "daily_budget": int(round(plan.daily_budget_brl * 100)),
                "billing_event": "IMPRESSIONS", "optimization_goal": "CONVERSATIONS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "destination_type": "WHATSAPP",
                "promoted_object": {"page_id": page_id, "whatsapp_phone_number": plan.phone},
                "targeting": {
                    "geo_locations": {"cities": [{"key": city_key}]},
                    "age_min": plan.age_min, "age_max": plan.age_max,
                    "targeting_automation": {"advantage_audience": 1},
                },
            })
            out.adset_id = str(adset["id"])
            out.steps.append("Conjunto de anúncios criado (pausado)")

            welcome = {
                "type": "VISUAL_EDITOR", "version": 2, "landing_screen_type": "welcome_message", "media_type": "text",
                "text_format": {"customer_action_type": "ice_breakers", "message": {
                    "ice_breakers": [], "quick_replies": [], "text": plan.welcome_text}},
                "user_edit": False, "surface": "visual_editor_new",
            }
            link = "https://api.whatsapp.com/send?" + urllib.parse.urlencode({"phone": plan.phone})
            for ad in plan.ads:
                cta = {"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP", "link": link}}
                if ad.kind == "image":
                    story = {"page_id": page_id, "link_data": {
                        "image_hash": image_hash, "link": link, "message": ad.text, "name": ad.headline,
                        "description": ad.link_description, "call_to_action": cta,
                        "page_welcome_message": json.dumps(welcome, ensure_ascii=False)}}
                else:
                    story = {"page_id": page_id, "video_data": {
                        "video_id": video_id, "image_url": thumb, "message": ad.text, "title": ad.headline,
                        "link_description": ad.link_description, "call_to_action": cta,
                        "page_welcome_message": json.dumps(welcome, ensure_ascii=False)}}
                creative = _post(client, settings, f"{acct}/adcreatives", {
                    "name": ad.name, "object_story_spec": story,
                    "url_tags": f"utm_source=meta&utm_medium=paid_social&utm_campaign={plan.utm_campaign}&utm_content={ad.utm_content}",
                })
                made = _post(client, settings, f"{acct}/ads", {
                    "name": ad.name, "adset_id": out.adset_id, "status": PAUSED,
                    "creative": {"creative_id": str(creative["id"])},
                })
                out.ad_ids.append(str(made["id"]))
                out.steps.append("Anúncio criado (pausado): " + ad.name)
        except MetaAdsError as exc:
            done = "; ".join(out.steps) or "nada ainda"
            raise MetaAdsError(f"{exc} · Já criado antes do erro: {done}. Campanha: {out.campaign_id or '—'}.") from exc
    finally:
        if owns_client:
            client.close()
    return out
