from __future__ import annotations

ROLES = {
    "ADMIN": "Administrador",
    "GESTOR": "Gestor",
    "SUPERVISOR": "Supervisor",
    "CONSULTOR": "Consultor",
}

TEAMS = {
    "LOJA": "Loja / Receptivo",
    "DIGITAL": "Digital",
    "PAP": "Porta a porta",
    "B2B": "Corporativo B2B",
}

SOURCES = {
    "MANUAL": "Cadastro manual",
    "APIFY": "Prospecção automatizada",
    "TRAFEGO_PAGO": "Tráfego pago",
    "BASE_ATIVA": "Base ativa",
    "INDICACAO": "Indicação",
    "IMPORTACAO": "Importação",
}

CHANNELS = {
    "WHATSAPP": "WhatsApp",
    "TELEFONE": "Telefone",
    "EMAIL": "E-mail",
    "INSTAGRAM": "Instagram",
    "LOJA": "Loja física",
    "VISITA": "Visita",
}

STAGES = {
    "NOVO": "Novo",
    "TENTATIVA_CONTATO": "Tentativa de contato",
    "CONTATO_REALIZADO": "Contato realizado",
    "QUALIFICADO": "Qualificado",
    "PROPOSTA": "Proposta",
    "NEGOCIACAO": "Negociação",
    "ANALISE_CREDITO": "Análise de crédito",
    "VIABILIDADE": "Viabilidade",
    "CONTRATO": "Contrato",
    "BKO": "BKO / Auditoria",
    "AGENDAMENTO": "Agendamento",
    "INSTALACAO": "Instalação",
    "GANHO": "Ganho",
    "PERDIDO": "Perdido",
    "NAO_CONTATAR": "Não contatar",
}

PIPELINE_STAGES = [
    "NOVO",
    "TENTATIVA_CONTATO",
    "CONTATO_REALIZADO",
    "QUALIFICADO",
    "PROPOSTA",
    "NEGOCIACAO",
    "ANALISE_CREDITO",
    "VIABILIDADE",
    "CONTRATO",
    "BKO",
    "AGENDAMENTO",
    "INSTALACAO",
    "GANHO",
    "PERDIDO",
]

ACTIVITY_KINDS = {
    "WHATSAPP": "WhatsApp",
    "LIGACAO": "Ligação",
    "EMAIL": "E-mail",
    "VISITA": "Visita",
    "REUNIAO": "Reunião",
    "PROPOSTA": "Proposta",
    "FOLLOW_UP": "Follow-up",
    "NOTA": "Anotação",
}

ACTIVITY_STATUSES = {
    "PLANEJADA": "Planejada",
    "CONCLUIDA": "Concluída",
    "CANCELADA": "Cancelada",
}

TEMPERATURES = {
    "FRIO": "Frio",
    "MORNO": "Morno",
    "QUENTE": "Quente",
}

DEFAULT_TARGETS = [
    ("LOJA", "ATIVIDADES", 40),
    ("DIGITAL", "ATIVIDADES", 60),
    ("PAP", "NOVOS_LEADS", 20),
    ("B2B", "VISITAS", 10),
]

COVERAGE_CITIES = {
    "Santa Rosa",
    "Santo Ângelo",
    "Três de Maio",
    "Horizontina",
    "Crissiumal",
    "Boa Vista do Buricá",
    "Cerro Largo",
    "Giruá",
}
