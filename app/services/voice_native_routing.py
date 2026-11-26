"""Rotas Native para chamadas de campanhas pelo ramal SIP 1804.

A operadora identifica o numero de origem pelo techprefix do destino SIP.
O numero do contato e a blacklist permanecem na forma original; somente
a parte 'usuario' do SIP INVITE recebe a mascara da Native.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from app.services.voice_test import BRAZIL_DDDS, validate_test_phone

# Mapeamento homologado com a TI para o tronco Native do ramal 1804.
NATIVE_ORIGIN_NUMBERS = {
    "8003": "5535118003",
    "8004": "5535118004",
    "8005": "5535118005",
    "8006": "5535118006",
    "8007": "5535118007",
}
NATIVE_TECHPREFIXES = tuple(NATIVE_ORIGIN_NUMBERS)
NATIVE_ROUTING_MODES = frozenset({"ACCOUNT", "NATIVE_ROUND_ROBIN", "NATIVE_FIXED"})
NATIVE_ACCOUNT_USERNAME = "1804"
DEFAULT_LOCAL_DDD = "55"


class NativeRoutingError(ValueError):
    """Configuracao invalida ou destino fora das mascaras autorizadas."""


def parse_native_techprefixes(values: Sequence[str] | str | None) -> tuple[str, ...]:
    """Valida e preserva ordem 8003..8007, sem duplicar prefixos."""
    if values is None:
        tokens: list[str] = []
    elif isinstance(values, str):
        tokens = [part.strip() for part in values.split(",") if part.strip()]
    else:
        tokens = [str(part).strip() for part in values]
    if len(tokens) != len(set(tokens)):
        raise NativeRoutingError("Techprefix repetido. Selecione cada linha uma unica vez.")
    if any(token not in NATIVE_ORIGIN_NUMBERS for token in tokens):
        raise NativeRoutingError("Techprefix fora dos cinco numeros autorizados pela Native.")
    return tuple(prefix for prefix in NATIVE_TECHPREFIXES if prefix in tokens)


def validate_native_campaign(
    mode: str,
    selected_prefixes: Sequence[str] | str | None,
    fixed_prefix: str,
    local_ddd: str,
    sip_username: str,
) -> tuple[str, str, str]:
    """Retorna modo, prefixos persistidos e DDD; ACCOUNT mantem rota legada."""
    routing_mode = (mode or "ACCOUNT").strip().upper()
    if routing_mode not in NATIVE_ROUTING_MODES:
        raise NativeRoutingError("Selecione um modo de saida SIP valido.")
    ddd = (local_ddd or "").strip()
    if not re.fullmatch(r"[0-9]{2}", ddd) or int(ddd) not in BRAZIL_DDDS:
        raise NativeRoutingError("Informe um DDD local brasileiro valido para a Native.")
    if routing_mode == "ACCOUNT":
        return routing_mode, "", ddd
    if (sip_username or "").strip() != NATIVE_ACCOUNT_USERNAME:
        raise NativeRoutingError("Rodizio Native requer a conta SIP do ramal 1804.")
    if routing_mode == "NATIVE_FIXED":
        chosen = parse_native_techprefixes([fixed_prefix])
    else:
        chosen = parse_native_techprefixes(selected_prefixes)
    if not chosen:
        raise NativeRoutingError("Selecione pelo menos um techprefix Native.")
    return routing_mode, ",".join(chosen), ddd


def native_prefixes_for_campaign(campaign) -> tuple[str, ...]:
    """Fail-closed: nunca retorna ao prefixo antigo se o modo Native falhar."""
    mode = getattr(campaign, "outbound_routing_mode", "ACCOUNT") or "ACCOUNT"
    if mode == "ACCOUNT":
        return ()
    if mode not in NATIVE_ROUTING_MODES:
        raise NativeRoutingError("Modo de rota Native desconhecido.")
    selected = parse_native_techprefixes(getattr(campaign, "native_techprefixes", ""))
    if not selected:
        raise NativeRoutingError("Campanha Native sem linhas de saida configuradas.")
    if mode == "NATIVE_FIXED" and len(selected) != 1:
        raise NativeRoutingError("Linha fixa Native precisa selecionar exatamente um prefixo.")
    return selected


def selected_native_prefix(campaign, completed_native_claims: int) -> str | None:
    """Rodizio persistente baseado em chamadas ja reservadas no banco.

    A contagem das tentativas Native e guardada em VoiceRecipient e nao se perde
    na pausa, retomada ou reinicio do worker.
    """
    lines = native_prefixes_for_campaign(campaign)
    if not lines:
        return None
    mode = campaign.outbound_routing_mode
    if mode == "NATIVE_FIXED":
        return lines[0]
    if completed_native_claims < 0:
        raise NativeRoutingError("Contagem de chamadas Native inconsistente.")
    return lines[completed_native_claims % len(lines)]


def native_dial_digits(phone: str, prefix: str, local_ddd: str = DEFAULT_LOCAL_DDD) -> str:
    """Aplica literalmente as quatro mascaras confirmadas pela Native.

    Local fixo:   prefix + [2-5]XXXXXXX
    Local celular:prefix + 9XXXXXXXX
    DDD fixo:     prefix + 0ZZ + [2-5]XXXXXXX
    DDD celular:  prefix + 0ZZ + 9XXXXXXXX

    Numero pode vir do CRM com ou sem DDI 55; a operadora nao recebe DDI.
    """
    if prefix not in NATIVE_ORIGIN_NUMBERS:
        raise NativeRoutingError("Techprefix Native nao cadastrado.")
    if not re.fullmatch(r"[0-9]{2}", local_ddd or "") or int(local_ddd) not in BRAZIL_DDDS:
        raise NativeRoutingError("DDD local Native invalido.")
    digits = validate_test_phone(phone)
    national = digits[2:] if digits.startswith("55") and len(digits) in (12, 13) else digits
    ddd, subscriber = national[:2], national[2:]
    if not (
        (len(subscriber) == 8 and subscriber[:1] in {"2", "3", "4", "5"})
        or (len(subscriber) == 9 and subscriber.startswith("9"))
    ):
        raise NativeRoutingError("Destino nao corresponde a mascara Native de fixo ou celular.")
    return prefix + ("" if ddd == local_ddd else "0" + ddd) + subscriber
