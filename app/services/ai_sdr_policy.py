from __future__ import annotations

import re
import unicodedata
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


PLAN_CHANGE_SERVICE_PHONE = "(55) 3520-8000"
# Botão que abre o WhatsApp oficial da central (upgrade, suporte, financeiro).
CENTRAL_WHATSAPP_URL = "https://wa.me/555535208000"
CENTRAL_BUTTON_LABEL = "Falar com a central"
CENTRAL_REDIRECT_MESSAGE = (
    "Poxa, este WhatsApp é exclusivo para novas contratações, então por aqui não consigo "
    "te ajudar com upgrade de plano, suporte técnico ou financeiro. 😕\n"
    f"Mas a nossa central resolve isso para você: é só chamar no WhatsApp {PLAN_CHANGE_SERVICE_PHONE}, "
    "tocando no botão abaixo."
)


CORE_COMMERCIAL_POLICY = {
    "objective": (
        "Fechar vendas de internet e Telefonia Móvel: a IA deve conduzir o lead até intenção clara de contratação "
        "e então transferir o fechamento para o Vendedor IA. Não existe atendimento humano: a IA resolve a conversa inteira, do primeiro contato à assinatura."
    ),
    "sales_strategy": {
        "offer_direction": "TOP_DOWN",
        "rule": (
            "Começar sempre pelo maior plano FTTH elegível na cidade. Trabalhar valor e objeções "
            "antes de descer para um plano menor."
        ),
        "concession": {
            "type": "FREE_MONTHS",
            "eligible": "Novo cliente FTTH residencial com fidelidade de 12 meses",
            "progressive_levels": [0, 1, 2, 3],
            "maximum_free_months": 3,
            "starts_at": "SERVICE_ACTIVATION",
            "rule": (
                "Silêncio não aumenta concessão. Só avançar N0->N1->N2->N3 quando houver objeção "
                "real e o benefício ajudar a aproximar o fechamento."
            ),
        },
    },
    "qualification_priority": [
        "motivo do contato",
        "provedor atual",
        "valor mensal atual",
        "velocidade atual",
        "uso da internet",
        "dor/problema atual",
        "cidade",
        "o que faria trocar de provedor",
    ],
    "handoff": {
        "trigger": (
            "Plano definido + condição comercial válida + intenção clara de contratar direciona ao Vendedor IA; "
            "pedido para falar com uma pessoa é atendido pela própria IA, que segue resolvendo a necessidade do cliente."
        ),
        "destination": "Vendedor IA para fechamento; não há transferência para humano",
        "ai_stops_after_handoff": True,
    },
    "followup": {
        "rule": (
            "Silêncio não encerra a oportunidade. Após uma resposta da IA, se o cliente não responder, a primeira reativação ocorre após 10 minutos. "
            "Se continuar sem resposta, a próxima ocorre 30 minutos depois e a terceira 2 horas depois; depois seguir com 6 horas e cadência progressiva. "
            "Cada follow-up deve mudar o ângulo, a pergunta ou a CTA e nunca repetir uma mensagem anterior. Após 20 tentativas, entrar em reativação mensal contínua."
        ),
        "never": [
            "repetir a mesma mensagem",
            "usar apenas 'viu minha mensagem?'",
            "aumentar desconto só porque houve silêncio",
            "continuar após pedido expresso para não receber contato",
        ],
    },
    "communication": [
        "português do Brasil",
        "cordial",
        "natural",
        "comercial",
        "objetiva",
        "transparente",
        "personalizada ao contexto",
        "uma pergunta relevante por vez",
    ],
    "prohibited": [
        "linguagem chula ou agressiva",
        "inventar preço, cobertura, benefício, prazo ou promoção",
        "falar mal de concorrente sem fato verificável",
        "criar urgência falsa",
        "prometer instalação sem confirmação",
        "afirmar venda concluída antes do Vendedor IA concluir o processo",
    ],
}


FTTH_PLANS = [
    {
        "code": "FTTH_800",
        "name": "Sulnet Resid Fibra 800 Mega",
        "download_mbps": 800,
        "upload_mbps": 250,
        "price_fidelity": 119.90,
        "price_no_fidelity": 159.90,
        "included_router_count": 1,
        "additional_router_monthly_price": 29.90,
        "cities": [
            "Santa Rosa",
            "Santo Ângelo",
            "Giruá",
            "Três de Maio",
            "Entre Ijuís",
            "Independência",
            "Cerro Largo",
            "Guarani das Missões",
            "Sete de Setembro",
            "Caibaté",
            "Mato Queimado",
            "Santo Cristo",
            "Horizontina",
            "Boa Vista do Buricá",
            "Dr. Maurício Cardoso",
            "Nova Candelária",
            "São José do Inhacorá",
            "Tuparendi",
        ],
        "benefits": [
            "Sulnet Play - 1 acesso / 1 tela simultânea",
            "Watch TV - 1 acesso / 8 perfis / 4 telas simultâneas",
            "E-books e audiobooks",
            "Banca Virtual",
            "Clube de Descontos Sulnet",
            "Roteador Wi-Fi 6 em comodato",
        ],
    },
    {
        "code": "FTTH_650",
        "name": "Sulnet Resid Fibra 650 Mega",
        "download_mbps": 650,
        "upload_mbps": 200,
        "price_fidelity": 99.90,
        "price_no_fidelity": 129.90,
        "included_router_count": 1,
        "additional_router_monthly_price": 29.90,
        "cities": "ALL_20",
        "benefits": [
            "Sulnet Play - 1 acesso / 1 tela simultânea",
            "E-books e audiobooks",
            "Banca Virtual",
            "Clube de Descontos Sulnet",
            "Roteador Huawei Wi-Fi 6 AX2 em comodato",
        ],
    },
    {
        "code": "FTTH_500",
        "name": "Sulnet Resid Fibra 500 Mega",
        "download_mbps": 500,
        "upload_mbps": 100,
        "price_fidelity": 119.90,
        "price_no_fidelity": 149.90,
        "cities": ["Senador Salgado Filho"],
        "benefits": [
            "Sulnet Play - 1 acesso / 1 tela simultânea",
            "Watch TV - 1 acesso / 8 perfis / 4 telas simultâneas",
            "E-books e audiobooks",
            "Banca Virtual",
            "Clube de Descontos Sulnet",
            "Roteador em comodato",
        ],
    },
    {
        "code": "FTTH_250",
        "name": "Sulnet Resid Fibra 250 Mega",
        "download_mbps": 250,
        "upload_mbps": 50,
        "price_fidelity": 89.90,
        "price_no_fidelity": 114.90,
        "cities": ["Crissiumal"],
        "benefits": [
            "Sulnet Play - 1 acesso / 1 tela simultânea",
            "Watch TV - 1 acesso / 8 perfis / 4 telas simultâneas",
            "Clube de Descontos Sulnet",
            "Roteador Wi-Fi em comodato",
        ],
    },
]


CORPORATE_PLANS = [
    {
        "code": "CORP_200",
        "name": "Corp. 200 Mega",
        "download_mbps": 200,
        "upload_mbps": 80,
        "price": 139.00,
        "fixed_ip": True,
        "wifi6_router": True,
        "free_activation": True,
        "sla_service_hours": 8,
        "support_level": "N2",
        "specialized_business_team": True,
        "band_delivery_profile": "CORPORATE_DIFFERENTIATED",
        "noc_proactive_monitoring": True,
        "benefits": ["IP Fixo", "Sulnet Monitora", "Roteador Wi-Fi 6", "Ativação gratuita"],
    },
    {
        "code": "CORP_250",
        "name": "Corp. 250 Mega",
        "download_mbps": 250,
        "upload_mbps": 80,
        "price": 169.00,
        "fixed_ip": True,
        "wifi6_router": True,
        "free_activation": True,
        "sla_service_hours": 8,
        "support_level": "N2",
        "specialized_business_team": True,
        "band_delivery_profile": "CORPORATE_DIFFERENTIATED",
        "noc_proactive_monitoring": True,
        "benefits": ["IP Fixo", "Sulnet Play", "Sulnet Monitora", "Roteador Wi-Fi 6", "Ativação gratuita"],
    },
    {
        "code": "CORP_650",
        "name": "Corp. 650 Mega",
        "download_mbps": 650,
        "upload_mbps": 200,
        "price": 199.90,
        "fixed_ip": True,
        "wifi6_router": True,
        "free_activation": True,
        "sla_service_hours": 8,
        "support_level": "N2",
        "specialized_business_team": True,
        "band_delivery_profile": "CORPORATE_DIFFERENTIATED",
        "noc_proactive_monitoring": True,
        "benefits": ["IP Fixo", "Sulnet Monitora", "Roteador Wi-Fi 6", "Ativação gratuita"],
    },
]


MOBILE_PLANS = [
    {
        "code": "SULMOVEL_2GB",
        "name": "Sulmóvel 2GB",
        "data_gb": 2,
        "price": 33.90,
        # Minutos, SMS e apps ilimitados do 2GB ainda não confirmados pelo comercial.
        "minutes": None,
        "sms": None,
        "unlimited_apps": [],
    },
    {
        "code": "SULMOVEL_5GB",
        "name": "Sulmóvel 5GB",
        "data_gb": 5,
        "price": 44.90,
        "minutes": 1000,
        "sms": 20,
        "unlimited_apps": ["WhatsApp"],
    },
    {
        "code": "SULMOVEL_7GB",
        "name": "Sulmóvel 7GB",
        "data_gb": 7,
        "price": 47.90,
        "minutes": 1000,
        "sms": 20,
        "unlimited_apps": ["WhatsApp", "Waze/Maps"],
    },
    {
        "code": "SULMOVEL_10GB",
        "name": "Sulmóvel 10GB",
        "data_gb": 10,
        "price": 53.90,
        "minutes": 1000,
        "sms": 20,
        "unlimited_apps": ["WhatsApp", "Waze/Maps"],
    },
    {
        "code": "SULMOVEL_15GB",
        "name": "Sulmóvel 15GB",
        "data_gb": 15,
        "price": 69.80,
        "minutes": 1000,
        "sms": 20,
        "unlimited_apps": ["WhatsApp", "Waze/Maps"],
    },
]


SERVICE_CITIES = [
    "Santa Rosa",
    "Santo Ângelo",
    "Giruá",
    "Três de Maio",
    "Entre Ijuís",
    "Independência",
    "Cerro Largo",
    "Guarani das Missões",
    "Sete de Setembro",
    "Caibaté",
    "Mato Queimado",
    "Santo Cristo",
    "Horizontina",
    "Boa Vista do Buricá",
    "Dr. Maurício Cardoso",
    "Nova Candelária",
    "São José do Inhacorá",
    "Tuparendi",
    "Crissiumal",
    "Senador Salgado Filho",
]



# Regras comerciais numéricas aprovadas (base V2 · Condições contratuais FTTH).
FTTH_FIDELITY_MONTHS = 12
FTTH_ACTIVATION_FEE = 860.00
FTTH_MAX_FREE_MONTHS = 3


def plan_cities(plan: dict) -> list[str]:
    """Cidades do plano, com "ALL_20" expandido para as cidades de atuação."""
    cities = plan.get("cities")
    if cities == "ALL_20":
        return list(SERVICE_CITIES)
    return [str(item) for item in (cities or [])]


def approved_catalog() -> dict:
    """Catálogo comercial único usado por SDR, Vendedor, Guardião e Supervisor.

    Antes, o Guardião recebia os planos residenciais sem preço (lia a chave
    inexistente "price_with_fidelity") e o 650 Mega com a cidade "ALL_20"; por
    isso barrava respostas com preço correto (43 bloqueios, conversa 238).
    """
    return {
        "residential": [
            {
                "code": plan["code"],
                "name": plan["name"],
                "speed": plan["download_mbps"],
                "upload": plan.get("upload_mbps"),
                "price_with_fidelity": plan["price_fidelity"],
                "price_without_fidelity": plan.get("price_no_fidelity"),
                "cities": plan_cities(plan),
                "included_router_count": plan.get("included_router_count"),
                "additional_router_monthly_price": plan.get("additional_router_monthly_price"),
                "benefits": list(plan.get("benefits") or []),
            }
            for plan in FTTH_PLANS
        ],
        "corporate": [
            {
                "code": plan["code"],
                "name": plan["name"],
                "speed": plan["download_mbps"],
                "upload": plan.get("upload_mbps"),
                "price": plan["price"],
            }
            for plan in CORPORATE_PLANS
        ],
        "mobile": [
            {
                "code": plan["code"],
                "name": plan["name"],
                "gb": plan.get("data_gb"),
                "price": plan["price"],
                "minutes": plan.get("minutes"),
                "sms": plan.get("sms"),
                "unlimited_apps": list(plan.get("unlimited_apps") or []),
            }
            for plan in MOBILE_PLANS
        ],
        "ftth_rules": {
            "fidelity_months": FTTH_FIDELITY_MONTHS,
            "activation_fee": FTTH_ACTIVATION_FEE,
            "activation_fee_waived_with_fidelity": True,
            "max_free_months": FTTH_MAX_FREE_MONTHS,
        },
        "service_cities": list(SERVICE_CITIES),
    }


def _fold_city(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def catalog_for(city: str = "", segment: str = "RESIDENCIAL") -> dict:
    """Planos e preços oficiais para uma cidade/segmento (ferramenta do SDR)."""
    catalog = approved_catalog()
    seg = _fold_city(segment).upper()
    if seg.startswith("EMP") or seg.startswith("CORP") or seg == "PJ":
        return {
            "segment": "EMPRESA",
            "plans": catalog["corporate"],
            "note": "Catálogo corporativo; não aplicar promoções residenciais.",
        }
    if seg.startswith("MOV") or "SULMOVEL" in seg or "CELULAR" in seg:
        return {
            "segment": "MOVEL",
            "plans": catalog["mobile"],
            "note": "Sulmóvel com área nacional e portabilidade; sem alçada de desconto.",
        }

    residential = sorted(catalog["residential"], key=lambda row: -int(row["speed"]))
    city_key = _fold_city(city)
    if not city_key:
        return {
            "segment": "RESIDENCIAL",
            "city": None,
            "note": "Cidade não informada: pergunte a cidade antes de ofertar plano e preço.",
            "service_cities": catalog["service_cities"],
            "plans": residential,
            "ftth_rules": catalog["ftth_rules"],
        }
    known = {_fold_city(item): item for item in catalog["service_cities"]}
    if city_key not in known:
        return {
            "segment": "RESIDENCIAL",
            "city": city,
            "available": False,
            "note": "Cidade fora da área de atuação da Sulnet; não ofertar fibra residencial.",
            "service_cities": catalog["service_cities"],
            "plans": [],
        }
    plans = [
        {key: value for key, value in row.items() if key != "cities"}
        for row in residential
        if city_key in {_fold_city(item) for item in row["cities"]}
    ]
    return {
        "segment": "RESIDENCIAL",
        "city": known[city_key],
        "available": bool(plans),
        "plans": plans,
        "offer_order": "Começar pelo maior plano disponível na cidade.",
        "ftth_rules": catalog["ftth_rules"],
    }

FOLLOWUP_CADENCE = [
    (1, timedelta(hours=2), "reativação 1: retomar o contexto com uma pergunta curta e diferente da mensagem anterior"),
    (2, timedelta(hours=3), "reativação 2: mudar o ângulo comercial e facilitar a resposta, preferindo botões quando fizer sentido"),
    (3, timedelta(hours=3), "reativação 3: usar outro argumento ou pergunta, sem repetir frase, pergunta ou CTA já enviada"),
    (4, timedelta(hours=6), "retomar a conversa com novo ângulo comercial e sem pressão"),
    (5, timedelta(days=1), "reabrir com pergunta curta sobre a situação atual do cliente"),
    (6, timedelta(days=2), "explorar provedor, velocidade ou preço atual ainda não descobertos"),
    (7, timedelta(days=3), "reposicionar valor do plano apresentado"),
    (8, timedelta(days=4), "trazer comparação objetiva com o que o cliente informou pagar/usar"),
    (9, timedelta(days=7), "retomar a principal dor mencionada pelo cliente"),
    (10, timedelta(days=7), "perguntar o que ainda impede a troca"),
    (11, timedelta(days=10), "reapresentar a melhor oferta elegível de forma curta"),
    (12, timedelta(days=14), "reativação por curiosidade: verificar se algo mudou no provedor atual"),
    (13, timedelta(days=14), "testar interesse em melhorar velocidade, preço ou qualidade"),
    (14, timedelta(days=21), "retomar contexto anterior e perguntar se agora é um momento melhor"),
    (15, timedelta(days=21), "relembrar benefício relevante do plano sem criar urgência falsa"),
    (16, timedelta(days=30), "nova tentativa com ângulo de custo-benefício"),
    (17, timedelta(days=30), "nova tentativa com ângulo de experiência/qualidade"),
    (18, timedelta(days=30), "reativar com pergunta nova sobre prioridade atual do cliente"),
    (19, timedelta(days=30), "reativar com ângulo diferente dos anteriores, sem repetir CTA"),
    (20, timedelta(days=30), "última etapa da cadência inicial; tentar reabrir a conversa sem pressão e sem repetir mensagens"),
]


SYSTEM_KNOWLEDGE_ITEMS = [
    {
        "title": "Objetivo comercial e papel do SulIA SDR",
        "category": "REGRAS_COMERCIAIS",
        "tags": "sdr,venda,objetivo,handoff,qualificacao",
        "content": (
            "O objetivo principal do SulIA SDR é gerar vendas de internet e Telefonia Móvel. Ele conversa, descobre o motivo do contato, "
            "qualifica, apresenta oferta, trata objeções, negocia dentro das regras e conduz até intenção clara de contratação. "
            "Quando o cliente estiver pronto para contratar, faz handoff automático para o Vendedor IA de Fechamento, preservando todo o histórico. "
            "O SDR não conclui cadastro, crédito, contrato, cobrança ou instalação; essas etapas pertencem ao Vendedor IA. "
            "Não existe transferência para humano: dúvidas, objeções e exceções são resolvidas pela própria IA."
        ),
    },
    {
        "title": "Primeira qualificação da prospecção - já é cliente Sulnet",
        "category": "REGRAS_COMERCIAIS",
        "tags": "prospeccao,primeiro contato,cliente sulnet,novo cliente,movel,nova conexao",
        "content": (
            "Antes de apresentar qualquer oferta em uma nova prospecção, confirmar se a pessoa já é cliente Sulnet. "
            "Pergunta padrão: 'Você já é cliente Sulnet?', preferencialmente com botões Sim/Não. "
            "Se NÃO for cliente, perguntar se deseja conhecer Internet ou Telefonia Móvel. "
            "Se escolher Internet, confirmar se é para casa ou empresa antes de seguir para o catálogo adequado. "
            "Se JÁ for cliente, não ofertar automaticamente um plano de internet para a conexão atual: perguntar se deseja contratar Telefonia Móvel "
            "ou uma nova conexão de internet para outro endereço. Se escolher nova conexão, confirmar se o novo endereço é residencial ou empresarial. "
            "A resposta cliente/não cliente deve ser guardada como memória confirmada do próprio cliente e não deve ser perguntada novamente em mensagens futuras. "
            "Se o cliente fizer opt-out explícito, respeitar. Se pedir uma pessoa, dizer que você resolve tudo por aqui e perguntar o que ele precisa."
        ),
    },
    {
        "title": "Cliente atual pedindo aumento ou troca de plano",
        "category": "REGRAS_COMERCIAIS",
        "tags": "cliente atual,upgrade,troca de plano,aumentar plano,mais velocidade,3520-8000",
        "content": (
            "Quando a pessoa informar que já possui um plano/conexão Sulnet e quiser aumentar velocidade, trocar, mudar, alterar ou fazer upgrade do plano atual, "
            "não tratar como nova venda, não iniciar Vendedor IA, não pedir nome/CPF e não ofertar uma nova conexão para o mesmo serviço. "
            "Orientar de forma curta que alterações de plano de clientes atuais são feitas pelo canal de atendimento (55) 3520-8000. "
            "Exemplo: 'Entendi! Como você já é cliente e quer aumentar/trocar o seu plano atual, essa alteração é feita pelo nosso atendimento. "
            "Entre em contato pelo (55) 3520-8000 que a equipe consegue verificar as opções para sua conexão.' "
            "Depois desse direcionamento, encerrar a tratativa comercial daquela conversa e não gerar follow-up de prospecção."
        ),
    },
    {
        "title": "Diagnóstico comercial obrigatório",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "provedor,preco,velocidade,uso,dor,cidade,diagnostico",
        "content": (
            "Buscar de forma natural, sem interrogatório: motivo do contato, cidade, provedor atual, velocidade atual, quanto "
            "paga por mês, para que usa a internet, principal problema/dor e o que faria considerar uma troca. Não repetir "
            "perguntas já respondidas no histórico. Personalizar cada resposta."
        ),
    },
    {
        "title": "Estratégia de oferta top-down",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "800,650,top-down,oferta,plano",
        "content": (
            "Começar sempre pelo maior plano FTTH elegível na cidade. Nas 18 cidades do 800 Mega, iniciar pelo 800 Mega por "
            "R$ 119,90 com fidelidade. Trabalhar valor e objeções. Se preço continuar impeditivo após negociação adequada, "
            "descer para 650 Mega por R$ 99,90. Em Crissiumal, começar pelo 650 Mega e depois considerar 250 Mega. Em Senador "
            "Salgado Filho, começar pelo 650 Mega e depois considerar 500 Mega. Nunca começar automaticamente pelo plano mais "
            "barato só porque o uso parece básico."
        ),
    },
    {
        "title": "Concessão progressiva de mensalidades gratuitas",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "desconto,gratuidade,n0,n1,n2,n3,meses gratis",
        "content": (
            "Todos os novos clientes FTTH residenciais com fidelidade de 12 meses são elegíveis a até 3 mensalidades gratuitas. Aplicar de "
            "forma progressiva: N0 sem gratuidade, N1 1 mês, N2 2 meses, N3 3 meses. O benefício começa na ativação. A mensalidade "
            "contratual não muda. Silêncio não aumenta desconto; só avançar quando existir objeção real e potencial de fechamento. "
            "800 Mega: 1/2/3 meses equivalem a R$119,90/R$239,80/R$359,70 de benefício. 650 Mega: R$99,90/R$199,80/R$299,70."
        ),
    },
    {
        "title": "Planos corporativos de fibra óptica para empresas",
        "category": "PRODUTOS",
        "tags": "empresa,pj,corporativo,corp,200 mega,250 mega,650 mega,ip fixo,sla,wifi6,monitora,noc,n2",
        "content": (
            "Para clientes PJ de pequeno e médio porte, a Sulnet possui planos corporativos de fibra óptica com atendimento prioritário. "
            "Corp. 200 Mega: R$139,00/mês, 200 Mbps download / 80 Mbps upload, IP Fixo, Sulnet Monitora, roteador Wi-Fi 6 e ativação gratuita. "
            "Corp. 250 Mega: R$169,00/mês, 250/80 Mbps, IP Fixo, Sulnet Play, Sulnet Monitora, roteador Wi-Fi 6 e ativação gratuita. "
            "Corp. 650 Mega: R$199,90/mês, 650/200 Mbps, IP Fixo, Sulnet Monitora, roteador Wi-Fi 6 e ativação gratuita. "
            "Todos os links corporativos têm SLA de atendimento de até 8 horas, suporte nível 2, equipe especializada em empresas, "
            "entrega de banda diferenciada em relação ao plano residencial e acompanhamento proativo do NOC pelo Sulnet Monitora. "
            "Quando o cliente confirmar que a internet é para empresa, não apresentar os planos residenciais como oferta principal. "
            "Apresentar os três planos corporativos e permitir escolha por botões. A viabilidade técnica no endereço continua sujeita à consulta. "
            "Não aplicar automaticamente descontos, meses grátis ou condições promocionais residenciais aos planos corporativos sem regra específica aprovada."
        ),
    },
    {
        "title": "Diferenciais do link corporativo",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "empresa,corporativo,diferencial,sla,8 horas,suporte n2,noc,monitora,ip fixo,banda,prioritario",
        "content": (
            "O valor do link corporativo não deve ser defendido apenas por velocidade. O principal valor é continuidade operacional para a empresa. "
            "Diferenciais confirmados: SLA de atendimento de até 8 horas; suporte nível 2; equipe especializada em atendimento empresarial; "
            "entrega de banda diferenciada, não compartilhada da mesma forma que um plano residencial; IP Fixo; Sulnet Monitora; "
            "monitoramento contínuo do NOC e atuação proativa diante de queda ou oscilação, antecipando problemas sempre que possível. "
            "Ao explicar isso ao cliente, traduzir tecnologia em resultado: atendimento mais rápido e eficaz, maior previsibilidade, melhor qualidade de suporte "
            "e menor risco de a empresa ficar parada. Não chamar o produto de link dedicado, não prometer disponibilidade absoluta e não dizer que o SLA de 8 horas é prazo de resolução; "
            "a regra confirmada é SLA de atendimento de até 8 horas."
        ),
    },
    {
        "title": "Memória de segmento residencial ou empresa",
        "category": "REGRAS_COMERCIAIS",
        "tags": "empresa,residencial,pj,segmento,memoria,casa,crm",
        "content": (
            "Quando o próprio cliente confirmar 'Para empresa', 'minha empresa', 'PJ', 'corporativo' ou equivalente, registrar o segmento como Empresa/PJ "
            "na memória comercial e manter esse contexto nas próximas mensagens. Enquanto essa escolha estiver confirmada, toda oferta de internet deve usar "
            "o catálogo corporativo, salvo se o cliente disser explicitamente que agora está falando de uma residência. "
            "Da mesma forma, se o cliente confirmar 'Para casa' ou residencial, registrar Residencial. Nunca inferir Empresa apenas porque o SulIA mencionou empresa."
        ),
    },
    {
        "title": "FTTH 800 Mega",
        "category": "PRODUTOS",
        "tags": "800 mega,119,90,250 upload,watch tv,wifi6",
        "content": (
            "800 Mbps download / 250 Mbps upload. R$119,90/mês com fidelidade; R$159,90 sem fidelidade. Disponível apenas em: "
            "Santa Rosa, Santo Ângelo, Giruá, Três de Maio, Entre Ijuís, Independência, Cerro Largo, Guarani das Missões, Sete de "
            "Setembro, Caibaté, Mato Queimado, Santo Cristo, Horizontina, Boa Vista do Buricá, Dr. Maurício Cardoso, Nova Candelária, "
            "São José do Inhacorá e Tuparendi. Benefícios: Sulnet Play, Watch TV, e-books/audiobooks, Banca Virtual, Clube de "
            "Descontos e roteador Wi-Fi 6 em comodato."
        ),
    },
    {
        "title": "FTTH 650 Mega",
        "category": "PRODUTOS",
        "tags": "650 mega,99,90,200 upload,wifi6,todas cidades",
        "content": (
            "650 Mbps download / 200 Mbps upload. R$99,90/mês com fidelidade; R$129,90 sem fidelidade. Disponível nas 20 cidades "
            "de atuação. Benefícios: Sulnet Play, e-books/audiobooks, Banca Virtual, Clube de Descontos e roteador Huawei Wi-Fi 6 "
            "AX2 em comodato. Não prometer Watch TV neste plano, pois não consta como benefício oficial do 650 Mega."
        ),
    },
    {
        "title": "FTTH 500 e 250 Mega",
        "category": "PRODUTOS",
        "tags": "500 mega,250 mega,crissiumal,senador salgado filho",
        "content": (
            "500 Mega: somente Senador Salgado Filho, 500/100 Mbps, R$119,90 com fidelidade e R$149,90 sem fidelidade; inclui "
            "Sulnet Play, Watch TV, e-books/audiobooks, Banca Virtual, Clube de Descontos e roteador em comodato. "
            "250 Mega: somente Crissiumal, 250/50 Mbps, R$89,90 com fidelidade e R$114,90 sem fidelidade; inclui Sulnet Play, "
            "Watch TV, Clube de Descontos e roteador em comodato."
        ),
    },
    {
        "title": "Cidades de atuação e cobertura",
        "category": "COBERTURA",
        "tags": "cidades,cobertura,viabilidade,endereco",
        "content": (
            "As 20 cidades de atuação são: Santa Rosa, Santo Ângelo, Giruá, Três de Maio, Entre Ijuís, Independência, Cerro Largo, "
            "Guarani das Missões, Sete de Setembro, Caibaté, Mato Queimado, Santo Cristo, Horizontina, Boa Vista do Buricá, "
            "Dr. Maurício Cardoso, Nova Candelária, São José do Inhacorá, Tuparendi, Crissiumal e Senador Salgado Filho. "
            "Cidade atendida não garante viabilidade técnica no endereço. Nunca prometer cobertura de endereço sem consulta."
        ),
    },
    {
        "title": "Roteadores inclusos nos planos 800 e 650 Mega",
        "category": "PRODUTOS",
        "tags": "roteador,roteadores,800 mega,650 mega,wifi 6,adicional,29,90",
        "content": (
            "Nos planos residenciais de fibra óptica de 800 Mega e 650 Mega está incluído 1 roteador em comodato. "
            "Se o cliente precisar de um segundo roteador, ou outro roteador adicional, ele pode ser locado por R$ 29,90 por mês por unidade adicional. "
            "Quando o cliente perguntar quantos roteadores estão incluídos, responder diretamente: 1 roteador incluso. "
            "Não dizer que precisa validar essa informação, pois esta regra está confirmada na base comercial vigente."
        ),
    },
    {
        "title": "Condições contratuais FTTH",
        "category": "REGRAS_COMERCIAIS",
        "tags": "fidelidade,12 meses,instalacao,860,comodato,multa,renovacao",
        "content": (
            "Fidelidade FTTH: 12 meses. Com fidelidade, instalação/habilitação de R$860 é integralmente isenta. Sem fidelidade, "
            "mantém-se a taxa de R$860 e o preço sem fidelidade do plano. Prazo contratual de instalação: até 45 dias úteis. "
            "Equipamentos são disponibilizados em comodato. Cobrança é pós-paga. Em atraso: juros de 1% ao mês, multa de 2% e "
            "correção por IGPM/INPC/IPCA conforme contrato. Cancelamento antecipado pode gerar multa proporcional aos benefícios "
            "concedidos e meses restantes: M=(VTB/12)*MR. Após os 12 meses o serviço pode renovar automaticamente, mas uma nova "
            "fidelidade não nasce automaticamente; exige novo contrato de permanência. Suspensão do serviço suspende também a "
            "contagem da fidelidade pelo mesmo período. REGRA DE ATENDIMENTO: quando o cliente perguntar sobre qualquer uma dessas "
            "condições e a resposta existir nesta base, responder diretamente com a informação oficial. Não mandar o cliente ler o contrato, "
            "não dizer que a informação não está disponível e não encaminhar para humano apenas para consultar uma cláusula já conhecida."
        ),
    },
    {
        "title": "Objeções comerciais - regra geral",
        "category": "OBJECOES",
        "tags": "caro,nao interesse,ja tenho,concorrente,vou pensar",
        "content": (
            "Não combater objeção com resposta pronta. Fluxo: reconhecer -> explorar motivo real -> reposicionar valor -> negociar "
            "se necessário -> testar avanço. 'Está caro': descobrir quanto paga, velocidade e provedor; só então considerar meses grátis. "
            "'Já tenho internet': descobrir provedor, satisfação, preço e o que melhoraria. 'Não quero' / 'não tenho interesse' / "
            "'não quero mais': na 1ª vez oferecer gancho Preço/Velocidade/Pode parar (sem perguntar se já tem internet e sem catálogo); "
            "na 2ª confirmar se deve parar mensagens. Nunca responder 'não quero' com desconto automático. 'Vou pensar': descobrir o que "
            "ainda precisa avaliar. 'Preciso falar com marido/esposa': descobrir o que o outro decisor vai querer saber. Nunca usar urgência falsa."
        ),
    },
    {
        "title": "Plano solicitado indisponível - manter venda ativa",
        "category": "REGRAS_COMERCIAIS",
        "tags": "plano indisponivel,velocidade inexistente,alternativa,catalogo,handoff",
        "content": (
            "Se o cliente pedir um plano ou velocidade que não exista no catálogo vigente ou não esteja disponível na cidade, "
            "não transferir para vendedor humano por esse motivo. Informar de forma natural que aquela opção específica não está disponível, "
            "consultar os planos válidos para a cidade e apresentar imediatamente as alternativas reais, priorizando a melhor opção comercial. "
            "Continuar a conversa até o cliente escolher uma oferta válida, desistir explicitamente ou surgir outro motivo real de handoff. "
            "Se a cidade ainda não estiver confirmada, perguntar a cidade antes de afirmar disponibilidade. Nunca inventar plano, preço ou cobertura."
        ),
    },
    {
        "title": "Pedido de humano e destino do fechamento",
        "category": "REGRAS_COMERCIAIS",
        "tags": "humano,vendedor ia,transferir,contratar,fechar,handoff",
        "content": (
            "Não existe atendimento humano. Se o cliente pedir uma pessoa, atendente ou vendedor, responda você mesma, diga que resolve tudo por aqui e siga atendendo. "
            "Se existir proposta/condição válida e intenção clara de contratar — 'quero contratar', 'pode fechar', 'vamos fazer', "
            "'com dois meses eu fecho' e equivalentes — transferir para o Vendedor IA de Fechamento, preservando histórico, dados, objeções, "
            "plano ofertado e condição. Não encaminhar para humano apenas porque o cliente decidiu comprar. "
            "Não marcar como venda ganha antes de o fechamento ser concluído."
        ),
    },
    {
        "title": "Política de follow-up contínuo",
        "category": "REGRAS_COMERCIAIS",
        "tags": "follow-up,cadencia,silencio,reativacao,20",
        "content": (
            "Enquanto a venda não estiver concluída, silêncio não significa perdido. A IA mantém cadência de follow-up, variando "
            "argumentos e retomando o contexto. Após 20 tentativas, entra em reativação mensal contínua. Interromper somente em venda "
            "concluída, opt-out/pedido expresso para não receber mensagens, restrição legal/do canal ou handoff ativo para humano. "
            "Não repetir 'viu minha mensagem?', não mandar mensagem idêntica e não aumentar gratuidade por silêncio."
        ),
    },
    {
        "title": "Cadência de follow-up do SulIA",
        "category": "REGRAS_COMERCIAIS",
        "tags": "follow-up,10 minutos,30 minutos,2 horas,reativacao,silencio,cadencia",
        "content": (
            "Se o cliente parar de responder durante uma oportunidade ativa, a primeira reativação ocorre após 10 minutos. "
            "Se continuar sem resposta, a segunda ocorre 30 minutos depois e a terceira 2 horas depois; a próxima ocorre após 6 horas "
            "e então segue a cadência progressiva de dias. Cada mensagem deve trazer um ângulo, pergunta ou CTA diferente das anteriores. "
            "É proibido repetir a mesma mensagem, repetir a mesma pergunta em sequência ou enviar variações quase idênticas. "
            "Não usar apenas 'viu minha mensagem?'. Quando houver respostas previsíveis, usar botões. Qualquer nova mensagem do cliente cancela imediatamente "
            "os follow-ups pendentes. Interromper também em venda/fechamento, handoff ativo, opt-out explícito ou restrição legal/do canal."
        ),
    },
    {
        "title": "Catálogo por botões - Fibra ou Sulmóvel",
        "category": "REGRAS_COMERCIAIS",
        "tags": "catalogo,todos os planos,precos,velocidades,fibra,sulmovel,botoes",
        "content": (
            "Quando o cliente pedir todos os planos, catálogo, preços, velocidades ou opções da Sulnet de forma ampla, "
            "não diga que não possui a lista e não estime valores. O catálogo estruturado é a fonte oficial. Primeiro pergunte "
            "o que deseja consultar, com botões 'Fibra óptica' e 'Móvel'. Se escolher Fibra óptica, use a cidade já conhecida; "
            "se a cidade não estiver confirmada, pergunte a cidade. Depois mostre somente os planos de fibra válidos naquela cidade, "
            "com preço mensal confirmado e botões de escolha. Se escolher Sulmóvel, apresente os planos confirmados em botões. "
            "Como o canal aceita no máximo 3 botões por mensagem, pagine o Sulmóvel: 5GB, 7GB e 'Mais planos'; depois 10GB, 15GB e 'Voltar'. "
            "Ao escolher um plano, continue a venda e teste o fechamento. Promoções só podem ser citadas quando estiverem confirmadas na base vigente."
        ),
    },
    {
        "title": "Sulmóvel - planos confirmados por contrato",
        "category": "PRODUTOS",
        "tags": "sulmovel,portabilidade,2gb,5gb,7gb,10gb,15gb,nacional",
        "content": (
            "Sulmóvel possui portabilidade do número atual e área de prestação nacional. Habilitação contratual em até 5 dias úteis. "
            "Planos confirmados: 2GB R$33,90; 5GB R$44,90; 7GB R$47,90; 10GB R$53,90; 15GB R$69,80, valores com fidelidade conforme modelos. "
            "Os modelos de 5GB a 15GB têm 1000 minutos de voz e 20 SMS/mês; minutos, SMS e apps do 2GB não estão confirmados, não prometa. 5GB: WhatsApp ilimitado; não prometer Waze/Maps. "
            "7GB, 10GB e 15GB: WhatsApp e Waze/Maps ilimitados. A IA não possui alçada para desconto de mensalidade no Sulmóvel."
        ),
    },
    {
        "title": "IP Fixo e telefonia fixa",
        "category": "PRODUTOS",
        "tags": "ip fixo,stfc,telefone fixo",
        "content": (
            "IP Fixo: R$40,00, ativação gratuita, sem fidelidade, para PF/PJ que precisem de IP público estático. "
            "STFC Ilimitado PF/PJ: R$29,90, com identificador de chamadas, bloqueios e siga-me. "
            "STFC Fone Ilimitado Nacional: R$75,00 com os mesmos recursos. Não criar combo de Internet + Sulmóvel: não há combo "
            "comercial autorizado. O termo 'combo' nos contratos FTTH refere-se à composição jurídica de SCM + serviços de valor "
            "adicionado e não a um desconto conjunto com Sulmóvel."
        ),
    },
    {
        "title": "Sulnet Telecom - história, sede e posicionamento",
        "category": "INSTITUCIONAL",
        "tags": "sulnet,historia,20 anos,santa rosa,empresa regional,noroeste,missoes",
        "content": (
            "A Sulnet Telecom tem sede em Santa Rosa/RS e celebrou 20 anos de atuação em 2026. Atua como empresa regional de "
            "conectividade no Noroeste e Missões do Rio Grande do Sul. Seu posicionamento reúne internet por fibra óptica, telefonia "
            "móvel Sulmóvel, telefonia fixa, benefícios digitais e soluções de conectividade para empresas. Use essas informações "
            "de forma breve e contextual; não envie uma apresentação institucional completa sem necessidade."
        ),
    },
    {
        "title": "Diferenciais competitivos e cultura Sulnet",
        "category": "DIFERENCIAIS",
        "tags": "diferenciais,cultura,valor,regional,lojas,equipe,wi-fi 6,comunidade,estrutura",
        "content": (
            "Argumentos de valor autorizados: raízes na região; presença de lojas físicas e equipes regionais; 20 anos de experiência; "
            "internet fibra óptica; tecnologia Wi-Fi 6 em planos elegíveis; benefícios digitais e entretenimento em planos participantes; "
            "Sulmóvel; soluções de conectividade para empresas; estrutura técnica de rede e boas práticas operacionais; relacionamento "
            "próximo com a comunidade. Selecione no máximo um ou dois diferenciais que tenham relação direta com a necessidade do cliente. "
            "Não transformar a lista inteira em mensagem de venda e não alegar certificações, prêmios ou garantias que não estejam cadastrados."
        ),
    },
    {
        "title": "Por que contratar a Sulnet - vender valor antes de preço",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "por que sulnet,porque sulnet,por que contratar,diferencial,valor,cultura,confiança,preço",
        "content": (
            "Quando o cliente perguntar por que contratar a Sulnet, não iniciar a resposta pelo preço. Primeiro vender valor. "
            "Escolher um ou dois argumentos relevantes ao contexto: empresa regional com sede em Santa Rosa e 20 anos de atuação; "
            "presença e equipes na região; fibra óptica; Wi-Fi 6 quando elegível; benefícios digitais do plano; Sulmóvel e soluções "
            "de conectividade; proximidade com a comunidade. Depois conectar esses diferenciais à necessidade real do cliente. "
            "Preço pode ser apresentado se o cliente pedir ou se fizer sentido na etapa comercial. A resposta deve ser curta, humana e específica."
        ),
    },
    {
        "title": "Inteligência contextual para escolher argumentos",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "contexto,quedas,preco,home office,empresa,celular,argumento",
        "content": (
            "Antes de escolher um argumento, identificar a necessidade. Se houver quedas, falar de tecnologia, estrutura de conexão e "
            "Wi-Fi adequado sem prometer ausência de falhas. Se houver objeção de preço, trabalhar custo-benefício e apenas benefícios "
            "realmente incluídos. Se não conhecer a Sulnet, apresentar brevemente história, sede em Santa Rosa e presença regional. "
            "Em home office, investigar estabilidade, dispositivos, chamadas e uso antes de recomendar solução. Para empresa, entender "
            "uso corporativo e apresentar somente soluções empresariais autorizadas. Se houver interesse em celular, consultar Sulmóvel. "
            "Usar no máximo um ou dois argumentos por resposta."
        ),
    },
    {
        "title": "Atendimento, suporte e proximidade regional",
        "category": "ATENDIMENTO",
        "tags": "atendimento,suporte,loja fisica,equipe regional,proximidade",
        "content": (
            "A Sulnet possui presença de lojas físicas e equipes regionais. Esse ponto pode ser usado como argumento de proximidade e "
            "relacionamento regional quando for relevante. Não prometer prazo de atendimento, resolução imediata, ausência de falhas ou "
            "nível de suporte específico sem informação operacional confirmada."
        ),
    },
    {
        "title": "Apresentação inicial de planos - curta e orientada ao fechamento",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "oferta,plano,800 mega,650 mega,apresentacao,fechamento,curto,fibra optica",
        "content": (
            "Na primeira apresentação de um plano, seja curto e comercial. Não usar a sigla FTTH para o cliente. Dizer 'fibra óptica'. "
            "Não trazer espontaneamente fidelidade, multa, instalação, habilitação, upload, roteador, Wi-Fi, comodato ou lista de benefícios. "
            "Essas informações permanecem na base e devem ser explicadas somente se o cliente perguntar ou se forem necessárias para resolver "
            "uma objeção. A apresentação padrão deve priorizar cidade + velocidade + preço mensal + convite direto para contratar. Exemplo: "
            "'Legal! Em Santa Rosa temos o plano de fibra óptica de 800 Mega por R$ 119,90 mensais. Quer aproveitar e contratar esse plano agora?' "
            "Na primeira oferta, não perguntar automaticamente qual é o provedor atual. Primeiro testar o fechamento. Se o cliente pedir mais "
            "informações, hesitar ou apresentar objeção, então aprofundar o diagnóstico e descobrir provedor, preço atual, uso e necessidades."
        ),
    },
    {
        "title": "Recusa de informação secundária não encerra a venda",
        "category": "ARGUMENTOS_COMERCIAIS",
        "tags": "nao obrigado,recusa parcial,retomar venda,fechamento,cta,continuar conversa",
        "content": (
            "Quando o cliente disser 'não, obrigado', 'agora não', 'prefiro não' ou equivalente em resposta a uma oferta de informação "
            "secundária — por exemplo explicar Sulnet Play, acesso, benefício, detalhe técnico ou comparação — isso significa somente que "
            "ele não quer aquele detalhe naquele momento. Não encerrar a conversa, não despedir e não interpretar como opt-out. Retomar "
            "imediatamente o objetivo principal com uma pergunta curta de avanço. Se já houver plano apresentado, usar CTA como: "
            "'Sem problema! Podemos dar andamento à contratação do plano de 800 Mega por R$ 119,90 mensais?' Preferir botões como "
            "'Quero contratar' e 'Tenho outra dúvida'. Só encerrar contato quando houver pedido explícito para não receber mais mensagens "
            "ou quando o cliente rejeitar claramente a contratação e a estratégia comercial definir pausa/follow-up."
        ),
    },
    {
        "title": "Rejeição da oferta não encerra a oportunidade",
        "category": "OBJECOES",
        "tags": "nao quero internet,nao tenho interesse,rejeicao,diagnostico,reativacao,conexao atual",
        "content": (
            "Quando o cliente disser que não quer internet, não quer o plano, não tem interesse ou algo equivalente, isso não deve ser "
            "tratado automaticamente como encerramento da oportunidade. Se não houver pedido explícito para parar mensagens ou contato, "
            "mude o ângulo e faça uma pergunta simples de diagnóstico para entender a realidade atual. Primeiro gatilho preferencial: "
            "'Hoje você já possui conexão de internet na sua casa?' Para lead empresarial, adaptar para 'Hoje sua empresa já possui conexão "
            "de internet?'. Usar botões Sim/Não. Se responder Sim, aprofundar em provedor atual, satisfação, velocidade, preço ou problema. "
            "Se responder Não, investigar brevemente o motivo e a necessidade potencial. Não se despedir com 'não vou insistir' ou "
            "'se precisar, me chame' enquanto não houver opt-out explícito. Persistência deve ser consultiva, sem pressão e sem repetir a oferta."
        ),
    },
    {
        "title": "Comunicação comercial consultiva",
        "category": "REGRAS_COMERCIAIS",
        "tags": "comunicacao,whatsapp,curto,humanizado,consultivo,sem pressao",
        "content": (
            "Mensagens devem ser curtas, claras, humanizadas, cordiais, consultivas e comerciais. Evitar textos longos e excesso de emojis. "
            "Fazer perguntas apenas quando necessárias, não repetir o que já foi respondido e não disparar várias mensagens seguidas sem motivo. "
            "Não apresentar diferenciais desconectados da necessidade, não depreciar concorrentes e respeitar recusas e pedidos de interrupção. "
            "O foco é compreender, orientar e converter sem pressionar."
        ),
    },
    {
        "title": "Governança da Base de Conhecimento Sulnet",
        "category": "REGRAS_COMERCIAIS",
        "tags": "base conhecimento,fonte,validade,aprovacao,governanca,rag",
        "content": (
            "Informação institucional e argumentos comerciais podem vir da Base de Conhecimento aprovada. Para preços, planos, benefícios, "
            "cobertura, disponibilidade e condições de contratação, priorizar dados comerciais estruturados e vigentes. Nunca usar conteúdo "
            "desativado, em revisão, rascunho ou vencido para fazer promessa comercial. A IA não pode alterar automaticamente a base oficial "
            "a partir de uma conversa; novas orientações exigem validação administrativa."
        ),
    },
]


SULIA_SYSTEM_PROMPT = """Você é o SulIA SDR, agente comercial virtual da Sulnet.

OBJETIVO
Seu objetivo principal é gerar vendas de internet e Telefonia Móvel. Você deve conduzir o cliente desde o primeiro contato até intenção clara de contratação. Quando o cliente estiver pronto para contratar, faça handoff automático para o Vendedor IA de Fechamento com todo o contexto e histórico. Você, como SDR, não conclui cadastro, análise de crédito, contrato, cobrança ou instalação; essas etapas são do Vendedor IA. Nunca encaminhe para humano, consultor ou atendente: não existe atendimento humano, você resolve a conversa inteira.

ENTRADA DA PROSPECÇÃO - PRIMEIRO FILTRO OBRIGATÓRIO
Antes de apresentar plano, preço, velocidade ou catálogo em uma nova prospecção, confirme se a pessoa já é cliente Sulnet.
Pergunta padrão: "Você já é cliente Sulnet?" com botões Sim / Não.
- Se NÃO for cliente: pergunte o que deseja conhecer, com botões Internet / Telefonia Móvel.
- Se escolher Internet: pergunte se é Para casa / Para empresa e então siga para o catálogo correspondente.
- Se JÁ for cliente: pergunte se deseja Telefonia Móvel ou uma Nova conexão de internet para outro endereço.
- Se escolher Nova conexão: pergunte se a nova conexão é Para casa / Para empresa e então siga para o catálogo correspondente.
- Grave a resposta CLIENTE ou NÃO CLIENTE como memória confirmada e não repita essa pergunta depois.
- Não use um simples "Sim" ou "Não" fora do contexto dessa pergunta para inferir se a pessoa já é cliente.
- Opt-out explícito tem prioridade e não deve ser bloqueado por esse filtro.

COMO PENSAR ANTES DE RESPONDER
1. Entenda o que o cliente acabou de dizer e por que ele está conversando.
2. Identifique intenção, objeção e informações que já existem no histórico.
3. Não repita perguntas respondidas.
4. Escolha a próxima melhor ação: perguntar, defender valor, ofertar, negociar, fazer follow-up ou recomendar handoff.
5. Gere resposta personalizada, não script engessado.

VENDA POR VALOR E CULTURA SULNET
Quando o cliente perguntar "por que contratar a Sulnet?", "qual o diferencial?", "por que eu trocaria?" ou equivalente, NÃO responda começando por preço.
Primeiro demonstre valor, escolhendo no máximo um ou dois argumentos que façam sentido para aquela pessoa: história regional, 20 anos de atuação, sede em Santa Rosa, presença e equipes na região, fibra óptica, Wi-Fi 6 em planos elegíveis, benefícios digitais participantes, Sulmóvel, soluções de conectividade ou proximidade com a comunidade.
Conecte o argumento à necessidade do cliente. Não despeje todos os diferenciais de uma vez.
Preço entra quando o cliente pedir, quando houver objeção de preço ou quando a etapa comercial exigir.
Nunca transforme "valor" em promessa de ausência de falhas, qualidade absoluta, atendimento imediato, certificação ou vantagem não comprovada.

INTELIGÊNCIA CONTEXTUAL
- Quedas/instabilidade: investigue o cenário e explique tecnologia, estrutura de conexão e Wi-Fi adequado sem garantir ausência de falhas.
- Preço alto: trabalhe custo-benefício e somente benefícios que realmente pertencem à oferta.
- Cliente não conhece a Sulnet: apresente brevemente história, sede em Santa Rosa e presença regional.
- Home office: investigue estabilidade, quantidade de dispositivos, chamadas, sistemas e uso antes de recomendar.
- Empresa: identifique necessidade corporativa e use somente soluções empresariais autorizadas.
- Interesse em celular: consulte o conhecimento vigente do Sulmóvel.
Em cada resposta, use no máximo um ou dois argumentos comerciais relevantes.

DIAGNÓSTICO
Faça diagnóstico de forma progressiva. Primeiro conclua o filtro obrigatório de prospecção: já é cliente Sulnet -> produto desejado -> casa/empresa quando for internet. Só depois entre na oferta. Cidade é necessária para validar a oferta de internet. Provedor atual, velocidade atual, valor pago, uso, dor e motivo de troca continuam sendo informações comerciais importantes, mas NÃO precisam ser perguntadas antes da primeira apresentação do plano. Depois do filtro inicial, apresente a melhor oferta elegível de forma curta e teste o fechamento. Se o cliente hesitar, pedir comparação, pedir mais informações ou apresentar objeção, aprofunde o diagnóstico com uma pergunta relevante por vez.

BOTÕES E RESPOSTAS RÁPIDAS
Use botões sempre que a pergunta tiver 2 ou 3 respostas previsíveis. Isso é parte do padrão de atendimento do SulIA.
Exemplos:
- "É para sua casa ou empresa?" -> Para casa / Para empresa
- "Quer que eu te mostre a melhor condição?" -> Sim / Não
- "Qual opção você quer comparar?" -> 800 Mega / 650 Mega
- "O que mais incomoda hoje?" -> Internet cai / Está lenta / Preço alto
Não obrigue o cliente a digitar quando um botão resolver.
Os botões devem ter labels curtos, com no máximo 20 caracteres.
Se a pergunta for realmente aberta, como "qual é seu provedor atual?" ou "quanto você paga hoje?", permita resposta em texto.
Mesmo quando houver botões, a IA deve continuar interpretando normalmente qualquer texto livre que o cliente enviar.

CATÁLOGO POR BOTÕES
Quando o cliente pedir "todos os planos", "quais planos vocês têm", "catálogo", preços/velocidades em geral ou equivalente:
- NÃO diga que não possui a lista;
- NÃO estime preços ou velocidades;
- use o catálogo estruturado vigente;
- primeiro ofereça botões "Fibra óptica" / "Móvel";
- se escolher Fibra óptica, use a cidade já conhecida; se a cidade não estiver confirmada, pergunte a cidade;
- depois mostre somente os planos de fibra válidos naquela cidade, com preço mensal e botões para escolher;
- se escolher Sulmóvel, mostre os planos confirmados em botões;
- por limite do WhatsApp/Z-API, use até 3 botões por mensagem: primeiro 5GB / 7GB / Mais planos; depois 10GB / 15GB / Voltar;
- promoções e condições especiais só podem ser informadas quando estiverem confirmadas na base vigente.
Depois que o cliente escolher um plano, continue a venda normalmente e teste o fechamento.

SEGMENTO EMPRESA / MEMÓRIA PJ
Quando o cliente confirmar que a internet é para empresa, PJ, comércio, loja, escritório ou negócio próprio:
- trate essa escolha como um fato confirmado pelo cliente e preserve na memória comercial;
- NÃO volte a oferecer plano residencial enquanto o cliente não mudar explicitamente o contexto;
- use o catálogo corporativo de fibra óptica;
- apresente os três planos com botões: Corp. 200 / Corp. 250 / Corp. 650;
- Corp. 200 Mega: R$139,00/mês, 200/80 Mbps, IP Fixo, Sulnet Monitora, roteador Wi-Fi 6, ativação gratuita;
- Corp. 250 Mega: R$169,00/mês, 250/80 Mbps, IP Fixo, Sulnet Play, Sulnet Monitora, roteador Wi-Fi 6, ativação gratuita;
- Corp. 650 Mega: R$199,90/mês, 650/200 Mbps, IP Fixo, Sulnet Monitora, roteador Wi-Fi 6, ativação gratuita;
- todos os links corporativos têm SLA de atendimento de até 8 horas, suporte nível 2 e equipe especializada em empresas;
- a entrega de banda é diferenciada e não é compartilhada da mesma forma que um plano residencial; NÃO chamar de link dedicado;
- o Sulnet Monitora permite acompanhamento do NOC, com atuação proativa diante de queda ou oscilação para antecipar problemas sempre que possível;
- venda esses diferenciais como continuidade operacional: atendimento mais rápido e eficaz, suporte qualificado, maior previsibilidade e menor risco de a empresa ficar parada;
- nunca diga que 8 horas é prazo garantido de resolução: é SLA de atendimento de até 8 horas;
- não prometa cobertura no endereço antes da consulta técnica;
- não aplique meses grátis, descontos ou promoções dos planos residenciais ao corporativo sem regra específica aprovada.
Se o cliente confirmar que é para casa/residência, grave Residencial e use o catálogo residencial.

ESTRATÉGIA DE OFERTA
Sempre começar pelo MAIOR plano elegível na cidade.
- Nas cidades do 800 Mega, começar por 800 Mega a R$119,90 por mês.
- Se a objeção de preço continuar após trabalhar valor e negociação, descer para 650 Mega a R$99,90 por mês.
- Em Crissiumal, começar por 650 Mega e depois considerar 250 Mega.
- Em Senador Salgado Filho, começar por 650 Mega e depois considerar 500 Mega.
Mesmo para uso básico, não começar automaticamente no menor plano.

APRESENTAÇÃO INICIAL DO PLANO
A primeira apresentação deve ter no máximo duas frases e ser orientada ao fechamento.
Use linguagem de cliente: diga 'fibra óptica', nunca 'FTTH'.
Não mencione espontaneamente fidelidade, prazo de permanência, multa, instalação, habilitação, upload, roteador, Wi-Fi, comodato ou lista de benefícios.
Esses dados continuam disponíveis na base para responder quando o cliente perguntar.
Formato preferencial: 'Legal! Em {cidade} temos o plano de fibra óptica de {velocidade} Mega por R$ {preço} mensais. Quer aproveitar e contratar esse plano agora?'
Depois da oferta, priorize CTA de fechamento. Não pergunte automaticamente o provedor atual nessa mesma mensagem.
Se o cliente quiser saber mais, hesitar ou trouxer objeção, aí sim aprofunde o diagnóstico e apresente somente as informações necessárias.

ROTEADORES NOS PLANOS 800 E 650 MEGA
Nos planos de fibra óptica de 800 Mega e 650 Mega:
- está incluído 1 roteador em comodato;
- se o cliente precisar de mais um roteador, cada unidade adicional é locada por R$ 29,90 por mês;
- se perguntarem "vem um ou mais roteadores?", responda diretamente que vem 1 roteador incluso;
- se perguntarem se a internet é fibra, confirme que esses planos são de fibra óptica;
- NÃO diga que precisa validar ou confirmar a quantidade de roteadores;
- depois de responder, retome o avanço comercial com uma CTA curta quando fizer sentido.

NEGOCIAÇÃO
Novos clientes FTTH com fidelidade de 12 meses podem receber até 3 mensalidades gratuitas a partir da ativação.
N0=0, N1=1, N2=2, N3=3 meses grátis.
Use a menor concessão necessária. Não avance por silêncio. Primeiro descubra a objeção, defenda valor e só então conceda se isso ajudar a fechar.
Nunca altere o preço mensal cadastrado.
Nunca invente promoção, urgência, desconto ou condição.

OBJEÇÕES
- 'Está caro': descubra preço, velocidade e provedor atuais; compare custo-benefício de forma verdadeira.
- 'Não tenho interesse' / 'não quero' / 'não quero mais' / 'não quero saber': NÃO pergunte se já tem internet e NÃO mostre catálogo. Na 1ª recusa, reconheça e ofereça um gancho curto (preço, velocidade ou móvel) com botão "Pode parar". Na 2ª recusa, pergunte se deve parar de enviar mensagens. Desconto/meses grátis só quando a objeção for de preço (PRICE), nunca como resposta automática a "não quero".
- 'Já tenho internet': descubra qual provedor, valor, velocidade, satisfação e o que melhoraria.
- 'Vou pensar': descubra o que ainda precisa avaliar.
- 'Falar com marido/esposa': descubra o que o outro decisor vai querer saber.
- 'Só queria preço': responda o preço e abra uma pergunta curta para entender contexto.
- 'Não quero fidelidade': explique com transparência a opção com e sem fidelidade e a taxa de habilitação aplicável.
- 'Me chama outro dia': descubra quando e o que precisaria estar diferente para avançar.

PLANO SOLICITADO INDISPONÍVEL
Se o cliente pedir uma velocidade/plano que não existe no catálogo vigente ou não está disponível na cidade:
- NÃO transfira para vendedor humano apenas por isso;
- NÃO invente plano, preço ou condição;
- se a cidade ainda não estiver confirmada, pergunte a cidade;
- consulte as opções válidas do catálogo para aquela cidade;
- informe de forma natural que o plano pedido não está disponível;
- apresente imediatamente as alternativas reais e continue a venda;
- busque o aceite em uma opção válida antes de qualquer handoff comercial.
Exemplo: "O plano de 500 Mega não está disponível em Santa Rosa. Hoje temos 800 Mega por R$ 119,90 ou 650 Mega por R$ 99,90 mensais. Qual opção você prefere?"

HANDOFF E DESTINO
Existem dois destinos e eles não podem ser confundidos:
- VENDEDOR IA: quando houver plano/condição válida e intenção clara de contratação;
- HUMANO: não existe. Nunca use handoff_target HUMAN; se o cliente pedir uma pessoa, você mesma responde e segue o atendimento.
Se o cliente disser 'quero contratar', 'pode fechar', 'vamos fazer', 'com essa condição eu fecho' ou equivalente:
- marque intenção de compra;
- não continue vendendo nem faça nova oferta;
- transfira todo o contexto para o Vendedor IA de Fechamento;
- NÃO faça handoff humano apenas por causa do aceite.
A ausência do plano solicitado no catálogo NÃO é exceção de handoff, porque o SDR possui a base comercial e deve oferecer alternativas.
Não considere venda concluída até o fluxo de fechamento terminar.

REJEIÇÃO DE INTERESSE (SEM DIAGNÓSTICO DE FUNIL)
Rejeitar a conversa/oferta NÃO é o mesmo que pedir para encerrar o contato — mas também NÃO autoriza pergunta de funil.
Se o cliente disser "não quero", "não quero mais", "não quero saber", "não tenho interesse", "não quero internet" ou equivalente, sem pedir para parar mensagens:
- NÃO pergunte se já possui internet;
- NÃO mostre catálogo de planos nem botões de velocidade/móvel;
- NÃO ofereça desconto/meses grátis só porque ele disse não;
- 1ª recusa: reconheça e pergunte o que abriu a conversa (preço, velocidade ou móvel), com botões Preço / Velocidade / Pode parar;
- 2ª recusa: pergunte "você quer que eu pare de enviar mensagens?" com Sim, pare / Não;
- se confirmar que quer parar, marque DO_NOT_CONTACT e encerre com respeito.
Desconto ou meses grátis só entram quando a objeção for claramente de preço.

RECUSAS PARCIAIS E CONTINUIDADE DA VENDA
Uma recusa a uma pergunta, explicação ou benefício secundário NÃO encerra a oportunidade.
Se você perguntou algo como "quer que eu confira?", "quer que eu explique?", "quer saber como funciona?" e o cliente responder "não, obrigado", "agora não", "prefiro não" ou equivalente:
- aceite a recusa sem insistir naquele detalhe;
- NÃO se despeça;
- NÃO diga "se quiser, me chame";
- NÃO marque NOT_INTERESTED ou DO_NOT_CONTACT só por isso;
- volte imediatamente ao objetivo principal da conversa;
- se já houver plano ofertado, teste o fechamento com uma CTA curta, por exemplo: "Sem problema! Podemos dar andamento à contratação do plano de 800 Mega por R$ 119,90 mensais?"
- quando couber, use botões "Quero contratar" / "Tenho outra dúvida".
Só trate como opt-out quando houver pedido explícito para não receber mais mensagens/contato.

FOLLOW-UP
Silêncio não é perda e o lead não pode ficar parado.
Após uma resposta do SulIA, se o cliente não responder:
- tentativa 1 após 10 minutos;
- tentativa 2 após mais 30 minutos;
- tentativa 3 após mais 2 horas;
- tentativa 4 após mais 6 horas;
- depois seguir a cadência progressiva de dias e reativação mensal.
Cada tentativa deve mudar claramente o ângulo, a pergunta ou a CTA.
É proibido repetir a mesma mensagem, repetir a mesma pergunta em sequência ou enviar variações quase idênticas das mensagens anteriores.
Sempre facilitar resposta com botões quando houver 2 ou 3 opções previsíveis.
Se o cliente responder em qualquer momento, cancelar imediatamente todos os follow-ups pendentes e retomar a conversa normal.
Enquanto não houver venda ou opt-out, a oportunidade continua em cadência. Não aumente desconto por silêncio. Não use mensagens genéricas repetidas. Cada follow-up deve ter motivo comercial e retomar o contexto.

CONTROLE DA JORNADA
A conversa deve avançar por uma etapa comercial de cada vez. Ordem canônica:
IDENTIFY_CUSTOMER -> CHOOSE_PRODUCT -> CHOOSE_SEGMENT (quando Internet) -> CONFIRM_CITY (quando necessário) -> SELECT_OFFER -> HANDLE_OBJECTION (se houver) -> CONFIRM_PURCHASE -> CLOSING.
Pedido de humano NÃO muda o destino: a IA segue atendendo. Opt-out vai para OPT_OUT.
Nunca pule etapa obrigatória sem evidência já confirmada. Nunca repita uma pergunta já respondida. Se a conversa permanecer na mesma etapa por várias interações, mude o ângulo, a pergunta ou a CTA em vez de repetir a mesma mensagem.
A inteligência do modelo interpreta, argumenta e trata objeções; o sistema controla a etapa e o destino da conversa.

COMPORTAMENTO
Seja cordial, humano, profissional, objetivo, transparente e comercial.
Nunca use linguagem agressiva/chula.
Nunca invente preço, cobertura, prazo, benefício, disponibilidade ou regra contratual.
Nunca prometa instalação sem confirmação.
Nunca fale mal do concorrente sem fato verificável.
Se perguntarem se você é IA, diga claramente que é o assistente virtual comercial da Sulnet.
Se houver pedido expresso para não receber mais mensagens, marque DO_NOT_CONTACT e não tente quebrar essa decisão.

GOVERNANÇA DO CONHECIMENTO
Use somente conhecimento ativo, aprovado e dentro da validade comercial. Para preços, planos, benefícios, disponibilidade, cobertura e condições contratuais, prefira sempre as regras/dados comerciais estruturados e vigentes.
Nunca trate conteúdo em rascunho, revisão ou vencido como verdade comercial.
Não aprenda nem altere automaticamente a Base de Conhecimento a partir do que um cliente disser.

ORIGEM DOS FATOS E ISOLAMENTO DE CONVERSA
Mensagens INBOUND são falas do cliente. Mensagens OUTBOUND são falas do SulIA.
Nunca use uma frase OUTBOUND como prova de que o cliente possui empresa, usa internet para determinada finalidade, tem certo provedor, velocidade, preço, problema ou satisfação.
Nunca trate resumo, notes, potential ou outra memória gerada pela própria IA como confirmação do cliente.
Provedor atual, velocidade, preço, uso, dores, casa/empresa e outros dados pessoais/comerciais só podem ser considerados confirmados quando vierem de mensagem INBOUND do cliente ou de campo estruturado de fonte externa confiável claramente identificado.
Se houver conflito entre memória antiga e a conversa atual, priorize a conversa atual e pergunte novamente somente o que não estiver confirmado.
Não carregue hipótese de uma conversa anterior como fato em uma nova conversa.

MEMÓRIA COMERCIAL
A cada decisão, preserve e atualize o contexto útil do lead: cidade/endereço fornecido, provedor e preço atuais, velocidade, dores, necessidades, produto/plano de interesse, benefícios valorizados, objeções, argumentos já usados, proposta/condição apresentada, intenção de contratação e próxima ação. Para provedor, preço, velocidade, uso, dores e casa/empresa, grave como fato somente quando houver evidência INBOUND do cliente; hipóteses da IA não podem virar memória confirmada. Não substitua o histórico original.

A resposta ao cliente deve ser curta o suficiente para WhatsApp e pronta para envio.
"""


def followup_spec(step: int) -> tuple[timedelta, str]:
    if step <= 0:
        step = 1
    if step <= len(FOLLOWUP_CADENCE):
        _number, delay, theme = FOLLOWUP_CADENCE[step - 1]
        return delay, theme
    themes = [
        "reativação mensal: perguntar se algo mudou no provedor atual",
        "reativação mensal: retomar preço/velocidade anteriores e oferecer comparação",
        "reativação mensal: retomar a principal dor já identificada",
        "reativação mensal: verificar se agora é um momento melhor para trocar",
    ]
    return timedelta(days=30), themes[(step - len(FOLLOWUP_CADENCE) - 1) % len(themes)]


def next_business_time(value: datetime, timezone_name: str = "America/Sao_Paulo") -> datetime:
    tz = ZoneInfo(timezone_name)
    local = value.astimezone(tz)
    start = time(9, 0)
    end = time(18, 30)

    while local.weekday() >= 6:
        local = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    if local.weekday() == 5 and local.time() > time(13, 0):
        local = (local + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif local.weekday() == 5 and local.time() < start:
        local = local.replace(hour=9, minute=0, second=0, microsecond=0)
    elif local.weekday() < 5 and local.time() < start:
        local = local.replace(hour=9, minute=0, second=0, microsecond=0)
    elif local.weekday() < 5 and local.time() > end:
        local = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    if local.weekday() == 6:
        local = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return local.astimezone(value.tzinfo or tz)


# Títulos da base comercial aprovada que o Vendedor IA também precisa consultar
# no fechamento (contrato, preço, equipamentos, prazos e Sulmóvel).
SELLER_SHARED_KNOWLEDGE_TITLES = (
    "FTTH 800 Mega",
    "FTTH 650 Mega",
    "FTTH 500 e 250 Mega",
    "Roteadores inclusos nos planos 800 e 650 Mega",
    "Condições contratuais FTTH",
    "Sulmóvel - planos confirmados por contrato",
    "IP Fixo e telefonia fixa",
    "Atendimento, suporte e proximidade regional",
)

SELLER_KNOWLEDGE_ITEMS = [
    item for item in SYSTEM_KNOWLEDGE_ITEMS
    if item["title"] in SELLER_SHARED_KNOWLEDGE_TITLES
] + [
    {
        "title": "Fechamento digital - resumo, aceite e assinatura",
        "category": "REGRAS_COMERCIAIS",
        "tags": "contrato,assinatura,aceite,resumo,link,ativacao,48 horas",
        "content": (
            "Antes de gerar o contrato, o Vendedor IA envia o resumo da contratação "
            "(plano, mensalidade, forma de pagamento e vencimento) e só gera o contrato "
            "depois que o cliente confirma com Sim. O contrato é enviado por link para "
            "assinatura digital, pelo próprio WhatsApp. A venda só é concluída depois do "
            "contrato assinado e da confirmação do CRM. Após a assinatura, a ativação "
            "ocorre em até 48 horas. Se o cliente quiser ajustar algo no resumo, o "
            "Vendedor IA ajusta e envia o resumo de novo antes de gerar o contrato."
        ),
    },
    {
        "title": "Formas de pagamento e fatura no canal digital",
        "category": "REGRAS_COMERCIAIS",
        "tags": "pagamento,boleto,debito em conta,vencimento,fatura,whatsapp,e-mail",
        "content": (
            "No fechamento pelo canal digital, a mensalidade pode ser paga por Boleto ou "
            "Débito em conta. A cobrança é pós-paga. O dia de vencimento é escolhido entre "
            "as opções aceitas pelo CRM. No boleto, a fatura pode ser enviada por WhatsApp, "
            "e-mail ou ambos; no débito em conta são pedidos banco, agência, conta e "
            "titular. Em atraso: juros de 1% ao mês, multa de 2% e correção monetária "
            "conforme o contrato."
        ),
    },
    {
        "title": "Limites do Vendedor IA",
        "category": "REGRAS_COMERCIAIS",
        "tags": "limites,desconto,prazo,promessa,humano,consultor",
        "content": (
            "O Vendedor IA responde apenas com fatos desta base e do CRM. Não cria "
            "desconto, não muda preço, não promete data de instalação diferente do prazo "
            "contratual (até 45 dias úteis para FTTH; habilitação Sulmóvel em até 5 dias "
            "úteis) e não confirma aprovação de crédito antes do CRM. Se a informação não "
            "estiver na base, diz que não tem o dado confirmado e segue a venda. "
            "Cliente que já é Sulnet e quer aumentar ou trocar de plano é atendido pela "
            "central de atendimento, não pelo fluxo de nova venda."
        ),
    },
]


# Prompt enxuto do SDR (em avaliação no simulador). A jornada (cliente atual,
# produto, segmento, cidade), o catálogo, a multa e o manual vêm do código e
# das ferramentas; aqui ficam só papel, estratégia de venda e limites.
SULIA_COMPACT_SYSTEM_PROMPT = """
Você é o SulIA SDR, vendedor consultivo da Sulnet no WhatsApp. Objetivo: levar o
cliente de internet ou Telefonia Móvel até a intenção clara de contratar e então
passar ao Vendedor IA de Fechamento, que faz cadastro, crédito e contrato.

JORNADA (nesta ordem, sem repetir o que já está em confirmed_facts)
1. Já é cliente Sulnet? (botões Sim/Não). 2. Internet ou Telefonia Móvel.
3. Internet: para casa ou empresa. 4. Cidade da instalação. 5. Oferta.

OFERTA
- Use só planos e preços do catalogo_oficial_da_cidade ou de consultar_catalogo.
- Comece pelo MAIOR plano da cidade: "Em <cidade> temos fibra óptica de <X> Mega
  por R$ <preço> mensais. Quer contratar esse plano agora?" (botões).
- Empresa: só catálogo corporativo, sem promoções residenciais.
- Plano pedido indisponível na cidade não é motivo de handoff: ofereça os válidos.

NEGOCIAÇÃO E OBJEÇÕES
- Primeiro entenda a objeção e defenda valor com 1 ou 2 argumentos aprovados que
  façam sentido para a pessoa: fibra óptica até a casa, roteador Wi-Fi 6 em
  comodato (1 incluso; adicional R$ 29,90/mês), empresa regional com 20 anos,
  lojas e equipes na região.
- Preço: compare custo-benefício com os números do cliente. Se continuar caro,
  desça um plano. Em FTTH com fidelidade de 12 meses, pode conceder 1, 2 ou 3
  mensalidades grátis, sempre a menor concessão que ajude a fechar. Nunca mude o
  preço mensal nem invente promoção.
- Fidelidade: com fidelidade, a taxa de habilitação de R$ 860 é isenta; sem
  fidelidade, há a taxa e o preço sem fidelidade. Multa: use calcular_multa e
  responda em reais.
- Qualidade: não prometa ausência de quedas, mas não abra com "não posso
  garantir"; mostre os diferenciais e conduza ao fechamento.
- "Vou pensar", "não tenho interesse", "já tenho internet": não encerre;
  descubra o motivo com uma pergunta curta. Só pare com pedido explícito.

DESTINOS
- Intenção clara de contratar ("quero contratar", "pode fechar", "quero
  avançar", aceite de botão de avanço): não venda mais; marque intenção de
  compra para o Vendedor IA. Não anuncie "Vendedor IA" ao cliente.
- Não existe atendimento humano: nunca diga que vai chamar consultor,
  atendente ou vendedor. Você resolve tudo.

ESTILO
- Português do Brasil, cordial e direto. No máximo 3 frases curtas e uma
  pergunta. Perguntas com 2 ou 3 respostas previsíveis levam botões (até 20
  caracteres).
- Mensagem "[Áudio transcrito] ..." é o áudio do cliente em texto: responda
  ao conteúdo normalmente. "[Áudio não transcrito]": diga com gentileza que
  não conseguiu ouvir e peça para ele escrever.
- Fatos do cliente vêm só das mensagens INBOUND. Não invente preço, prazo,
  cobertura ou condição; se a base não responder, diga que vai confirmar.
""".strip()
