"""Catálogo operacional das campanhas de tráfego pago (Meta → origem CRM).

Os textos padrão vêm dos anúncios WhatsApp ativos. Caracteres invisíveis
(ZWSP/ZWNJ/WJ/BOM) do Meta são preservados no match_text para separar
conjuntos PERFORMANCE quando o copy visível é idêntico.
"""
from __future__ import annotations

from typing import TypedDict


class OriginCampaignSpec(TypedDict):
    name: str
    match_text: str
    crm_origin: str
    link: str


# Fingerprints Meta (entre "go" e "staria"), preservados byte-a-byte.
_PERF_SANTA_ROSA_CIDADES = (
    "\ufeff\u200c\u200c\u200c\u200c\u200b\u200b\u200b\u2060"
    "\u200c\u200c\u200c\u200c\u200b\u200b\u200b\ufeff"
)
_PERF_SANTO_ANGELO = (
    "\ufeff\u200c\u200b\u200c\u200b\u200c\u200c\u200c\u2060"
    "\u200c\u200c\u200c\u200b\u200b\u200b\ufeff"
)
_LOCAL_AEROPORTO = (
    "\ufeff\u200c\u200c\u200c\u200b\u200c\u200b\u200b\u2060"
    "\u200c\u200c\u200c\u200b\u200b\u200c\u200b\ufeff"
)


# Uma entrada por texto distintivo. Campanhas Meta com o mesmo prefill
# (ex.: C01 e C04 Black Friday em Santo Ângelo; PERF Santa Rosa = Cidades)
# compartilham a mesma origem no CRM.
PAID_TRAFFIC_ORIGIN_CAMPAIGNS: tuple[OriginCampaignSpec, ...] = (
    {
        "name": "Meta · Santo Ângelo · Black November",
        "match_text": (
            "Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet "
            "em Santo Ângelo!"
        ),
        "crm_origin": "Facebook Ads · Santo Ângelo · Black November",
        "link": "",
    },
    {
        "name": "Meta · Santa Rosa · Black Friday",
        "match_text": (
            "Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet "
            "em Santa Rosa!"
        ),
        "crm_origin": "Facebook Ads · Santa Rosa · Black Friday",
        "link": "",
    },
    {
        "name": "Meta · Cidades secundárias · Black Friday",
        "match_text": (
            "Olá, vim do anúncio de BLACK NOVEMBER e quero assinar Sulnet!"
        ),
        "crm_origin": "Facebook Ads · Cidades secundárias · Black Friday",
        "link": "",
    },
    {
        "name": "Meta · Performance · Santa Rosa/Cidades",
        "match_text": (
            f"Olá, vim do anúncio e go{_PERF_SANTA_ROSA_CIDADES}"
            "staria de assinar sulnet!"
        ),
        "crm_origin": "Facebook Ads · Performance (Santa Rosa/Cidades)",
        "link": "",
    },
    {
        "name": "Meta · Santo Ângelo · Valor 650mb",
        "match_text": (
            "Olá! Tenho interesse em assinar 650mb por R$99,90 da Sulnet!"
        ),
        "crm_origin": "Facebook Ads · Santo Ângelo · Valor 650mb",
        "link": "",
    },
    {
        "name": "Meta · Santo Ângelo · Aeroporto",
        "match_text": (
            f"Olá, vim do anúncio do aeroporto e go{_LOCAL_AEROPORTO}"
            "staria de assinar Sulnet!"
        ),
        "crm_origin": "Facebook Ads · Santo Ângelo · Aeroporto",
        "link": "",
    },
    {
        "name": "Meta · Santo Ângelo · Performance",
        "match_text": (
            f"Olá, vim do anúncio e go{_PERF_SANTO_ANGELO}"
            "staria de assinar Sulnet!"
        ),
        "crm_origin": "Facebook Ads · Santo Ângelo · Performance",
        "link": "",
    },
)


# Nomes exatos que o CRM Comercial precisa ter em `origins`.
CRM_ORIGIN_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(item["crm_origin"] for item in PAID_TRAFFIC_ORIGIN_CAMPAIGNS)
)
