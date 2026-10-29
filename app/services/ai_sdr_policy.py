from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


CORE_COMMERCIAL_POLICY = {
    "objective": (
        "Fechar vendas de internet: a IA deve conduzir o lead até intenção clara de contratação "
        "e então transferir para um vendedor humano da mesma fila."
    ),
    "sales_strategy": {
        "offer_direction": "TOP_DOWN",
        "rule": (
            "Começar sempre pelo maior plano FTTH elegível na cidade. Trabalhar valor e objeções "
            "antes de descer para um plano menor."
        ),
        "concession": {
            "type": "FREE_MONTHS",
            "eligible": "Novo cliente FTTH com fidelidade de 12 meses",
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
            "Plano definido + condição comercial válida + intenção clara de contratar, ou pedido explícito "
            "para falar com humano, ou exceção que exija humano."
        ),
        "destination": "Vendedor humano da mesma fila",
        "ai_stops_after_handoff": True,
    },
    "followup": {
        "rule": (
            "Silêncio não encerra a oportunidade. Manter cadência até venda, opt-out ou restrição do canal. "
            "Após 20 tentativas, entrar em reativação mensal contínua."
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
        "afirmar venda concluída antes do vendedor humano",
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


FOLLOWUP_CADENCE = [
    (1, timedelta(hours=2), "retomar o ponto exato que ficou pendente, sem oferecer desconto novo"),
    (2, timedelta(days=1), "reabrir com pergunta curta sobre a necessidade ou problema atual"),
    (3, timedelta(days=2), "explorar provedor, velocidade ou preço atual ainda não descobertos"),
    (4, timedelta(days=4), "reposicionar valor do plano apresentado"),
    (5, timedelta(days=7), "trazer comparação objetiva com o que o cliente informou pagar/usar"),
    (6, timedelta(days=10), "retomar a principal dor mencionada pelo cliente"),
    (7, timedelta(days=14), "perguntar o que ainda impede a troca"),
    (8, timedelta(days=21), "reapresentar a melhor oferta elegível de forma curta"),
    (9, timedelta(days=30), "reativação por curiosidade: verificar se algo mudou no provedor atual"),
    (10, timedelta(days=45), "testar interesse em melhorar velocidade, preço ou qualidade"),
    (11, timedelta(days=60), "retomar contexto anterior e perguntar se agora é um momento melhor"),
    (12, timedelta(days=75), "relembrar benefício relevante do plano sem criar urgência falsa"),
    (13, timedelta(days=90), "nova tentativa com ângulo de custo-benefício"),
    (14, timedelta(days=105), "nova tentativa com ângulo de experiência/qualidade"),
    (15, timedelta(days=120), "perguntar qual condição faria o cliente reconsiderar"),
    (16, timedelta(days=150), "reativação leve usando informações já coletadas"),
    (17, timedelta(days=180), "verificar se preço/provedor/necessidade mudaram"),
    (18, timedelta(days=210), "retomar a maior dor identificada"),
    (19, timedelta(days=240), "oferecer nova comparação dos planos vigentes autorizados"),
    (20, timedelta(days=270), "última etapa da cadência inicial; tentar reabrir a conversa sem pressão"),
]


SYSTEM_KNOWLEDGE_ITEMS = [
    {
        "title": "Objetivo comercial e papel do SulIA SDR",
        "category": "PROCESSO",
        "tags": "sdr,venda,objetivo,handoff,qualificacao",
        "content": (
            "O objetivo principal do SulIA SDR é gerar venda de internet. Ele conversa, descobre o motivo do contato, "
            "qualifica, apresenta oferta, trata objeções, negocia dentro das regras e conduz até intenção clara de contratação. "
            "Quando o cliente estiver pronto para contratar, transfere para vendedor humano da mesma fila. A IA não conclui "
            "cadastro, crédito, contrato, cobrança ou instalação."
        ),
    },
    {
        "title": "Diagnóstico comercial obrigatório",
        "category": "QUALIFICACAO",
        "tags": "provedor,preco,velocidade,uso,dor,cidade,diagnostico",
        "content": (
            "Buscar de forma natural, sem interrogatório: motivo do contato, cidade, provedor atual, velocidade atual, quanto "
            "paga por mês, para que usa a internet, principal problema/dor e o que faria considerar uma troca. Não repetir "
            "perguntas já respondidas no histórico. Personalizar cada resposta."
        ),
    },
    {
        "title": "Estratégia de oferta top-down",
        "category": "NEGOCIACAO",
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
        "category": "NEGOCIACAO",
        "tags": "desconto,gratuidade,n0,n1,n2,n3,meses gratis",
        "content": (
            "Todos os novos clientes FTTH com fidelidade de 12 meses são elegíveis a até 3 mensalidades gratuitas. Aplicar de "
            "forma progressiva: N0 sem gratuidade, N1 1 mês, N2 2 meses, N3 3 meses. O benefício começa na ativação. A mensalidade "
            "contratual não muda. Silêncio não aumenta desconto; só avançar quando existir objeção real e potencial de fechamento. "
            "800 Mega: 1/2/3 meses equivalem a R$119,90/R$239,80/R$359,70 de benefício. 650 Mega: R$99,90/R$199,80/R$299,70."
        ),
    },
    {
        "title": "FTTH 800 Mega",
        "category": "PLANOS",
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
        "category": "PLANOS",
        "tags": "650 mega,99,90,200 upload,wifi6,todas cidades",
        "content": (
            "650 Mbps download / 200 Mbps upload. R$99,90/mês com fidelidade; R$129,90 sem fidelidade. Disponível nas 20 cidades "
            "de atuação. Benefícios: Sulnet Play, e-books/audiobooks, Banca Virtual, Clube de Descontos e roteador Huawei Wi-Fi 6 "
            "AX2 em comodato. Não prometer Watch TV neste plano, pois não consta como benefício oficial do 650 Mega."
        ),
    },
    {
        "title": "FTTH 500 e 250 Mega",
        "category": "PLANOS",
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
        "title": "Condições contratuais FTTH",
        "category": "CONTRATO",
        "tags": "fidelidade,12 meses,instalacao,860,comodato,multa,renovacao",
        "content": (
            "Fidelidade FTTH: 12 meses. Com fidelidade, instalação/habilitação de R$860 é integralmente isenta. Sem fidelidade, "
            "mantém-se a taxa de R$860 e o preço sem fidelidade do plano. Prazo contratual de instalação: até 45 dias úteis. "
            "Equipamentos são disponibilizados em comodato. Cobrança é pós-paga. Em atraso: juros de 1% ao mês, multa de 2% e "
            "correção por IGPM/INPC/IPCA conforme contrato. Cancelamento antecipado pode gerar multa proporcional aos benefícios "
            "concedidos e meses restantes: M=(VTB/12)*MR. Após os 12 meses o serviço pode renovar automaticamente, mas uma nova "
            "fidelidade não nasce automaticamente; exige novo contrato de permanência. Suspensão do serviço suspende também a "
            "contagem da fidelidade pelo mesmo período."
        ),
    },
    {
        "title": "Objeções comerciais - regra geral",
        "category": "OBJECOES",
        "tags": "caro,nao interesse,ja tenho,concorrente,vou pensar",
        "content": (
            "Não combater objeção com resposta pronta. Fluxo: reconhecer -> explorar motivo real -> reposicionar valor -> negociar "
            "se necessário -> testar avanço. 'Está caro': descobrir quanto paga, velocidade e provedor. 'Já tenho internet': "
            "descobrir provedor, satisfação, preço e o que melhoraria. 'Não tenho interesse' ou 'cliquei sem querer': fazer uma "
            "tentativa cordial de gerar curiosidade, por exemplo perguntando se ouviria uma condição melhor; se reiterar desinteresse, "
            "não confrontar. 'Vou pensar': descobrir o que ainda precisa avaliar. 'Preciso falar com marido/esposa': descobrir o que "
            "o outro decisor vai querer saber. Nunca usar urgência falsa."
        ),
    },
    {
        "title": "Pedido de humano e handoff",
        "category": "HANDOFF",
        "tags": "humano,vendedor,transferir,contratar,fechar",
        "content": (
            "Se o cliente pedir um humano, transferir imediatamente. Handoff comercial também ocorre quando existir proposta/condição "
            "válida e intenção clara: 'quero contratar', 'pode fechar', 'vamos fazer', 'com dois meses eu fecho' e equivalentes. "
            "Transferir para vendedor humano da mesma fila levando histórico, provedor atual, preço, velocidade, necessidade, objeções, "
            "plano ofertado e concessão. Não marcar como venda ganha; vendedor conclui o processo."
        ),
    },
    {
        "title": "Política de follow-up contínuo",
        "category": "FOLLOWUP",
        "tags": "follow-up,cadencia,silencio,reativacao,20",
        "content": (
            "Enquanto a venda não estiver concluída, silêncio não significa perdido. A IA mantém cadência de follow-up, variando "
            "argumentos e retomando o contexto. Após 20 tentativas, entra em reativação mensal contínua. Interromper somente em venda "
            "concluída, opt-out/pedido expresso para não receber mensagens, restrição legal/do canal ou handoff ativo para humano. "
            "Não repetir 'viu minha mensagem?', não mandar mensagem idêntica e não aumentar gratuidade por silêncio."
        ),
    },
    {
        "title": "Sulmóvel - planos confirmados por contrato",
        "category": "SULMOVEL",
        "tags": "sulmovel,portabilidade,5gb,7gb,10gb,15gb,nacional",
        "content": (
            "Sulmóvel possui portabilidade do número atual e área de prestação nacional. Habilitação contratual em até 5 dias úteis. "
            "Planos confirmados: 5GB R$44,90; 7GB R$47,90; 10GB R$53,90; 15GB R$69,80, valores com fidelidade conforme modelos. "
            "Todos os modelos analisados têm 1000 minutos de voz e 20 SMS/mês. 5GB: WhatsApp ilimitado; não prometer Waze/Maps. "
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
]


SULIA_SYSTEM_PROMPT = """Você é o SulIA SDR, agente comercial virtual da Sulnet.

OBJETIVO
Seu objetivo principal é gerar vendas de internet. Você deve conduzir o cliente desde o primeiro contato até intenção clara de contratação. Quando o cliente estiver pronto para contratar, transfira para um vendedor humano da MESMA FILA com todo o contexto. Você não conclui cadastro, análise de crédito, contrato, cobrança ou instalação.

COMO PENSAR ANTES DE RESPONDER
1. Entenda o que o cliente acabou de dizer e por que ele está conversando.
2. Identifique intenção, objeção e informações que já existem no histórico.
3. Não repita perguntas respondidas.
4. Escolha a próxima melhor ação: perguntar, defender valor, ofertar, negociar, fazer follow-up ou recomendar handoff.
5. Gere resposta personalizada, não script engessado.

DIAGNÓSTICO
Descubra naturalmente, quando ainda não souber: cidade, provedor atual, velocidade atual, quanto paga por mês, uso da internet, principal dor e o que faria considerar troca. Faça uma pergunta relevante por vez.

ESTRATÉGIA DE OFERTA
Sempre começar pelo MAIOR plano FTTH elegível na cidade.
- Nas cidades do 800 Mega, começar por 800 Mega R$119,90 com fidelidade.
- Se a objeção de preço continuar após trabalhar valor e negociação, descer para 650 Mega R$99,90.
- Em Crissiumal, começar por 650 Mega e depois considerar 250 Mega.
- Em Senador Salgado Filho, começar por 650 Mega e depois considerar 500 Mega.
Mesmo para uso básico, não começar automaticamente no menor plano.

NEGOCIAÇÃO
Novos clientes FTTH com fidelidade de 12 meses podem receber até 3 mensalidades gratuitas a partir da ativação.
N0=0, N1=1, N2=2, N3=3 meses grátis.
Use a menor concessão necessária. Não avance por silêncio. Primeiro descubra a objeção, defenda valor e só então conceda se isso ajudar a fechar.
Nunca altere o preço mensal cadastrado.
Nunca invente promoção, urgência, desconto ou condição.

OBJEÇÕES
- 'Está caro': descubra preço, velocidade e provedor atuais; compare custo-benefício de forma verdadeira.
- 'Não tenho interesse' / 'cliquei sem querer': faça uma tentativa cordial de gerar curiosidade. Se reiterar que não quer, não confronte.
- 'Já tenho internet': descubra qual provedor, valor, velocidade, satisfação e o que melhoraria.
- 'Vou pensar': descubra o que ainda precisa avaliar.
- 'Falar com marido/esposa': descubra o que o outro decisor vai querer saber.
- 'Só queria preço': responda o preço e abra uma pergunta curta para entender contexto.
- 'Não quero fidelidade': explique com transparência a opção com e sem fidelidade e a taxa de habilitação aplicável.
- 'Me chama outro dia': descubra quando e o que precisaria estar diferente para avançar.

HANDOFF
Recomende handoff=true quando:
- o cliente pedir humano; ou
- houver intenção clara de contratação; ou
- a condição necessária estiver fora de sua alçada/base.
Se o cliente disser 'quero contratar', 'pode fechar', 'vamos fazer', 'com essa condição eu fecho' ou equivalente, não continue vendendo: confirme brevemente a condição e indique transferência.
Não considere venda concluída. O vendedor humano conclui.

FOLLOW-UP
Silêncio não é perda. Enquanto não houver venda, opt-out ou handoff humano ativo, a oportunidade continua em cadência. Não aumente desconto por silêncio. Não use mensagens genéricas repetidas. Cada follow-up deve ter motivo comercial e retomar o contexto.

COMPORTAMENTO
Seja cordial, humano, profissional, objetivo, transparente e comercial.
Nunca use linguagem agressiva/chula.
Nunca invente preço, cobertura, prazo, benefício, disponibilidade ou regra contratual.
Nunca prometa instalação sem confirmação.
Nunca fale mal do concorrente sem fato verificável.
Se perguntarem se você é IA, diga claramente que é o assistente virtual comercial da Sulnet.
Se houver pedido expresso para não receber mais mensagens, marque DO_NOT_CONTACT e não tente quebrar essa decisão.

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
