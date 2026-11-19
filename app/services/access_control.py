"""Permissões por usuário do Prospecta: perfil continua sendo limite de segurança.

Usuários antigos (access_permissions NULL) conservam as regras originais por
perfil. Usuários configurados têm uma lista explícita de autorizações;
esconder menus é apenas UI: cada requisição também é verificada no servidor.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

# Nome de menu e ações que fazem sentido na operação comercial.
# "roles" é o teto de segurança: nenhum checkbox supera esses limites.
ALL_HUMAN_ROLES = ("CONSULTOR", "SUPERVISOR", "GESTOR", "ADMIN")
MANAGEMENT_ROLES = ("GESTOR", "ADMIN")
SUPERVISION_ROLES = ("SUPERVISOR", "GESTOR", "ADMIN")
ADMIN_ROLE = ("ADMIN",)

PERMISSION_GROUPS: tuple[dict[str, Any], ...] = (
    {"key": "dashboard", "title": "Visão geral", "description": "Painel inicial e indicadores",
     "items": [
         ("dashboard.view", "Visualizar painel", ALL_HUMAN_ROLES),
     ]},
    {"key": "leads", "title": "Leads e CRM", "description": "Oportunidades e prospecção comercial",
     "items": [
         ("leads.view", "Ver leads e oportunidades", ALL_HUMAN_ROLES),
         ("leads.create", "Criar novos leads", ALL_HUMAN_ROLES),
         ("leads.edit", "Atualizar etapas e atividades", ALL_HUMAN_ROLES),
         ("leads.export", "Exportar listas de leads", ALL_HUMAN_ROLES),
         ("leads.assign", "Distribuir leads para vendedores", SUPERVISION_ROLES),
     ]},
    {"key": "conversations", "title": "Conversas e filas WhatsApp",
     "description": "As filas visíveis dependem também dos vínculos reais do vendedor",
     "items": [
         ("conversations.view", "Acessar Conversas e filas atribuídas", ALL_HUMAN_ROLES),
         ("conversations.reply", "Responder, anexar mídias e atender", ALL_HUMAN_ROLES),
         ("conversations.transfer", "Assumir ou transferir atendimento", ALL_HUMAN_ROLES),
         ("conversations.blacklist", "Bloquear contato na conversa", ALL_HUMAN_ROLES),
         ("crm.register", "Registrar nome e telefone no CRM", ALL_HUMAN_ROLES),
         ("crm.credit", "Consultar CPF na oportunidade", ALL_HUMAN_ROLES),
         ("crm.contest", "Contestar análise de crédito", ALL_HUMAN_ROLES),
     ]},
    {"key": "prospecting", "title": "Prospecção",
     "description": "Rotinas de contatos e trabalhos comerciais",
     "items": [
         ("prospecting.view", "Visualizar prospecções", ALL_HUMAN_ROLES),
         ("prospecting.manage", "Criar e executar prospecções", ALL_HUMAN_ROLES),
     ]},
    {"key": "campaigns", "title": "Campanhas WhatsApp",
     "description": "Criativos, ofertas e disparos por WhatsApp",
     "items": [
         ("campaigns.view", "Ver campanhas", ALL_HUMAN_ROLES),
         ("campaigns.manage", "Criar e editar campanhas", ALL_HUMAN_ROLES),
         ("campaigns.approve", "Aprovar campanhas", MANAGEMENT_ROLES),
     ]},
    {"key": "dispatches", "title": "Disparos WhatsApp",
     "description": "Carregamento de listas e execução de disparos",
     "items": [
         ("dispatches.view", "Ver campanhas de disparo", ALL_HUMAN_ROLES),
         ("dispatches.manage", "Criar, iniciar, pausar e gerenciar disparos", ALL_HUMAN_ROLES),
     ]},
    {"key": "voice", "title": "Prospecta VOZ",
     "description": "Campanhas telefônicas e gestão da telefonia",
     "items": [
         ("voice.view", "Visualizar painel e campanhas VOZ", ALL_HUMAN_ROLES),
         ("voice.campaigns.manage", "Criar, editar e controlar campanhas VOZ", ALL_HUMAN_ROLES),
         ("voice.accounts.manage", "Gerenciar contas SIP, ramais e grupos", ADMIN_ROLE),
     ]},
    {"key": "ai", "title": "IA",
     "description": "Visibilidade não ativa atendimento automático da IA",
     "items": [
         ("ai.view", "Visualizar painéis e qualidade da IA", ALL_HUMAN_ROLES),
         ("ai.manage", "Gerenciar parâmetros e treinamentos da IA", MANAGEMENT_ROLES),
     ]},
    {"key": "reports", "title": "Relatórios",
     "description": "Indicadores operacionais e exportações",
     "items": [
         ("reports.view", "Ver relatórios WhatsApp", ALL_HUMAN_ROLES),
         ("reports.export", "Exportar conversas, arquivos e relatórios", ALL_HUMAN_ROLES),
         ("credit_map.view", "Consultar mapa de crédito", SUPERVISION_ROLES),
         ("credit_map.import", "Importar dados do mapa de crédito", MANAGEMENT_ROLES),
     ]},
    {"key": "blacklist", "title": "Blacklist global", "description": "Listas de bloqueio de contatos",
     "items": [
         ("blacklist.view", "Ver blacklist global", MANAGEMENT_ROLES),
         ("blacklist.manage", "Importar e incluir bloqueios", MANAGEMENT_ROLES),
     ]},
    {"key": "settings", "title": "Configurações",
     "description": "Controles do ambiente; ações críticas respeitam o perfil",
     "items": [
         ("settings.view", "Abrir Configurações", SUPERVISION_ROLES),
         ("settings.queues.view", "Ver filas e seus integrantes", SUPERVISION_ROLES),
         ("settings.queues.manage", "Gerenciar integrantes e filas", SUPERVISION_ROLES),
         ("settings.users.view", "Ver usuários e acessos", SUPERVISION_ROLES),
         ("settings.users.manage", "Criar, editar e desativar usuários", MANAGEMENT_ROLES),
         ("settings.operations.view", "Ver metas e unidades", SUPERVISION_ROLES),
         ("settings.integrations.view", "Ver integrações", SUPERVISION_ROLES),
         ("settings.audit.view", "Ver auditoria", SUPERVISION_ROLES),
         ("settings.connections.view", "Ver números e conexões Z-API", MANAGEMENT_ROLES),
         ("settings.connections.manage", "Alterar números e conexões Z-API", MANAGEMENT_ROLES),
     ]},
)

PERMISSION_ROLES: dict[str, frozenset[str]] = {
    code: frozenset(roles)
    for group in PERMISSION_GROUPS
    for code, _label, roles in group["items"]
}
PERMISSION_LABELS: dict[str, str] = {
    code: label
    for group in PERMISSION_GROUPS
    for code, label, _roles in group["items"]
}
ALL_PERMISSION_CODES = frozenset(PERMISSION_ROLES)

# Se um módulo estiver oculto, suas operações também não podem ser acessadas.
REQUIRES: dict[str, str] = {
    **{k: "leads.view" for k in ("leads.create", "leads.edit", "leads.export", "leads.assign")},
    **{k: "conversations.view" for k in (
        "conversations.reply", "conversations.transfer", "conversations.blacklist",
        "crm.register", "crm.credit", "crm.contest",
    )},
    "prospecting.manage": "prospecting.view",
    "campaigns.manage": "campaigns.view",
    "campaigns.approve": "campaigns.view",
    "dispatches.manage": "dispatches.view",
    "voice.campaigns.manage": "voice.view",
    "voice.accounts.manage": "voice.view",
    "ai.manage": "ai.view",
    "reports.export": "reports.view",
    "credit_map.import": "credit_map.view",
    "blacklist.manage": "blacklist.view",
    **{k: "settings.view" for k in (
        "settings.queues.view", "settings.users.view",
        "settings.operations.view", "settings.integrations.view",
        "settings.audit.view", "settings.connections.view",
    )},
    "settings.queues.manage": "settings.queues.view",
    "settings.users.manage": "settings.users.view",
    "settings.connections.manage": "settings.connections.view",
}

# Matriz histórica antes de existir a personalização; evita modificar acessos
# dos colaboradores atuais sem revisão explícita do administrador.
_BASE_CONSULTOR = {
    "dashboard.view", "leads.view", "leads.create", "leads.edit", "leads.export",
    "conversations.view", "conversations.reply", "conversations.transfer",
    "conversations.blacklist", "crm.register", "crm.credit", "crm.contest",
    "prospecting.view", "prospecting.manage", "campaigns.view", "campaigns.manage",
    "reports.view", "reports.export",
}
_BASE_SUPERVISOR = _BASE_CONSULTOR | {
    "leads.assign", "credit_map.view", "settings.view",
    "settings.queues.view", "settings.queues.manage",
    "settings.users.view", "settings.operations.view",
    "settings.integrations.view", "settings.audit.view",
}
_BASE_GESTOR = _BASE_SUPERVISOR | {
    "campaigns.approve", "dispatches.view", "dispatches.manage",
    "ai.view", "ai.manage", "blacklist.view", "blacklist.manage",
    "credit_map.import", "settings.users.manage",
    "settings.connections.view", "settings.connections.manage",
}
DEFAULT_BY_ROLE: dict[str, frozenset[str]] = {
    "CONSULTOR": frozenset(_BASE_CONSULTOR),
    "SUPERVISOR": frozenset(_BASE_SUPERVISOR),
    "GESTOR": frozenset(_BASE_GESTOR),
    "ADMIN": ALL_PERMISSION_CODES,
}


def role_default_permissions(role: str) -> frozenset[str]:
    return DEFAULT_BY_ROLE.get((role or "").upper(), frozenset())


def effective_permissions(user: Any) -> frozenset[str]:
    if user is None:
        return frozenset()
    role = str(getattr(user, "role", "") or "").upper()
    if role == "ADMIN":
        # Conta administrador permanece irrestrita; ninguém se tranca fora.
        return ALL_PERMISSION_CODES
    stored = getattr(user, "access_permissions", None)
    if stored is None:
        return role_default_permissions(role)
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        # Conteúdo inesperado = negar, jamais converter em acesso irrestrito.
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(
        code for code in parsed
        if isinstance(code, str) and role in PERMISSION_ROLES.get(code, ())
    )


def has_permission(user: Any, code: str) -> bool:
    if user is None or not getattr(user, "active", True):
        return False
    role = str(getattr(user, "role", "") or "").upper()
    if role not in PERMISSION_ROLES.get(code, ()):
        return False
    perms = effective_permissions(user)
    if code not in perms:
        return False
    parent = REQUIRES.get(code)
    # Alguns recursos têm pai e avô (e.g. configurações -> filas -> editar).
    while parent is not None:
        if parent not in perms or role not in PERMISSION_ROLES.get(parent, ()):
            return False
        parent = REQUIRES.get(parent)
    return True


def can_grant_permission(actor: Any, target_role: str, code: str) -> bool:
    return (
        target_role in PERMISSION_ROLES.get(code, ())
        and (actor.role == "ADMIN" or (
            actor.role == "GESTOR" and has_permission(actor, code)
        ))
    )


def validate_selection(actor: Any, target_role: str, selected: list[str]) -> str | None:
    """Valida antes de criar/editar usuário; devolve JSON ou None p/ admin."""
    if target_role == "ADMIN":
        if actor.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Somente administradores criam outros administradores.")
        return None
    if len(selected) > len(ALL_PERMISSION_CODES) * 2:
        raise HTTPException(status_code=400, detail="Quantidade de permissões inválida.")
    codes = set(selected)
    for code in codes:
        if not isinstance(code, str) or not can_grant_permission(actor, target_role, code):
            raise HTTPException(status_code=403, detail="Permissão fora do perfil ou sem autorização.")
    for code in codes:
        parent = REQUIRES.get(code)
        while parent:
            if parent not in codes:
                raise HTTPException(
                    status_code=400,
                    detail=f"A ação {PERMISSION_LABELS[code]} exige o acesso ao seu módulo.",
                )
            parent = REQUIRES.get(parent)
    if not any(code.endswith(".view") for code in codes):
        raise HTTPException(status_code=400, detail="Selecione ao menos um menu para o usuário.")
    return json.dumps(sorted(codes), ensure_ascii=False)


def permission_catalog(actor: Any, target_role: str) -> list[dict[str, Any]]:
    """Matriz a exibir com rótulos, escopo e marcação de autorização."""
    return [
        {
            "key": group["key"],
            "title": group["title"],
            "description": group["description"],
            "items": [
                {
                    "code": code,
                    "label": label,
                    "allowed_roles": list(roles),
                    "grantable": can_grant_permission(actor, target_role, code),
                    "parent": REQUIRES.get(code, ""),
                }
                for code, label, roles in group["items"]
            ],
        }
        for group in PERMISSION_GROUPS
    ]


def permission_for_request(path: str, method: str) -> str | None:
    """Mapa único: o guard de sessão barra URLs diretas, não apenas menus."""
    path = path.rstrip("/") or "/"
    method = method.upper()
    write = method not in {"GET", "HEAD", "OPTIONS"}

    if path == "/dashboard":
        return "dashboard.view"
    if path == "/conversations-intelligence":
        return "reports.view"
    if path.startswith("/api/conversations/"):
        return "conversations.view"
    if path == "/conversations" or path.startswith("/conversations/"):
        if write:
            if path.endswith("/register-crm"):
                return "crm.register"
            if path.endswith("/consult-cpf"):
                return "crm.credit"
            if path.endswith("/credit-contestation"):
                return "crm.contest"
            if path.endswith("/blacklist"):
                return "conversations.blacklist"
            if any(part in path for part in ("/transfer", "/claim", "/assign")):
                return "conversations.transfer"
            return "conversations.reply"
        return "conversations.view"
    if path == "/leads" or path.startswith("/leads/"):
        if path.endswith("/export.csv"):
            return "leads.export"
        if path == "/leads/new":
            return "leads.create"
        if write:
            if path.endswith("/assign"):
                return "leads.assign"
            if path == "/leads/new":
                return "leads.create"
            return "leads.edit"
        return "leads.view"
    if path == "/campaigns" or path.startswith("/campaigns/"):
        if write:
            return "campaigns.approve" if path.endswith("/approve") else "campaigns.manage"
        return "campaigns.view"
    if path == "/dispatches" or path.startswith("/dispatches/"):
        return "dispatches.manage" if write or path.endswith("/sample.csv") else "dispatches.view"
    if path == "/prospecting" or path.startswith("/prospecting/"):
        return "prospecting.manage" if write else "prospecting.view"
    if path == "/voice" or path.startswith("/voice/"):
        if path.startswith(("/voice/accounts", "/voice/groups")):
            return "voice.accounts.manage"
        if path.startswith("/voice/campaigns"):
            if write or path.endswith(("/new", "/edit", "/delete", "/resend", "/contacts/modelo.csv")):
                return "voice.campaigns.manage"
            return "voice.view"
        if write:
            return "voice.accounts.manage"
        return "voice.view"
    if path == "/ai" or path.startswith("/ai/"):
        return "ai.manage" if write else "ai.view"
    if path == "/blacklist" or path.startswith("/blacklist/"):
        return "blacklist.manage" if write else "blacklist.view"
    if path == "/reports/credit-map" or path.startswith("/api/reports/credit-map"):
        return "credit_map.import" if write else "credit_map.view"
    if path == "/reports" or path.startswith("/reports/"):
        return "reports.export" if "/export/" in path or path.endswith((".csv", ".pdf")) else "reports.view"
    if path == "/settings" or path.startswith("/settings/"):
        if path.startswith("/settings/users"):
            return "settings.users.manage" if write or path.endswith("/edit") else "settings.users.view"
        if path.startswith("/settings/queues"):
            return "settings.queues.manage" if write else "settings.queues.view"
        if path.startswith("/settings/whatsapp") or path.startswith("/settings/zapi"):
            return "settings.connections.manage" if write else "settings.connections.view"
        if path.startswith("/settings/operations"):
            return "settings.operations.view"
        if path.startswith("/settings/integrations"):
            return "settings.integrations.view"
        if path.startswith("/settings/audit"):
            return "settings.audit.view"
        return "settings.view"
    if path == "/queues" or path.startswith("/queues/"):
        return "settings.queues.manage" if write else "settings.queues.view"
    return None


_HOME_ROUTES = (
    ("dashboard.view", "/dashboard"),
    ("conversations.view", "/conversations"),
    ("leads.view", "/leads"),
    ("prospecting.view", "/prospecting"),
    ("campaigns.view", "/campaigns"),
    ("dispatches.view", "/dispatches"),
    ("voice.view", "/voice"),
    ("reports.view", "/reports/whatsapp"),
    ("ai.view", "/ai"),
    ("settings.view", "/settings"),
)


def first_accessible_page(user: Any) -> str:
    return next((url for code, url in _HOME_ROUTES if has_permission(user, code)), "/change-password")
