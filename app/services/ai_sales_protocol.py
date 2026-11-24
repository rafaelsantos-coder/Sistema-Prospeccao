"""Protocolo comercial SulIA v2026-11-23, baseado em casos históricos anonimizados.

Não contém nomes, CPF, telefone, conversas ou preços especiais dos exemplos.
Apenas classifica a mensagem da conversa atual e disponibiliza instruções
validadas. CRM, assinatura, OS e ativação seguem suas APIs oficiais.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from zoneinfo import ZoneInfo

PROTOCOL_VERSION = "2026-11-23"
BRAZIL = ZoneInfo("America/Sao_Paulo")

OBSERVABLE_STAGES = (
    "NOVO_CONTATO",
    "INTERESSE_CONFIRMADO",
    "DIAGNOSTICO",
    "PROPOSTA_APRESENTADA",
    "OBJECAO_PENDENTE",
    "ACORDO_COMERCIAL",
    "CRM_REGISTRADO",
    "CONSULTA_CREDITO",
    "CONTRATO_ENVIADO",
    "ASSINATURA_CONFIRMADA",
    "INSTALACAO_AGENDADA",
    "CONTRATO_ATIVO",
    "PERDIDO",
    "TRANSFERIDO_A_HUMANO",
)

BASELINE_RULES = (
    "PROTOCOLO COMERCIAL SULIA 2026-11-23 (regras de seguranca e evidencia, "
    "prioritarias em relacao a exemplos antigos de vendas): "
    "responda primeiro ao que o cliente perguntou e nunca reinicie uma etapa ja respondida. "
    "Use apenas o contexto da conversa atual, dentro da fila e da instancia autorizadas; "
    "nunca acesse historico confidencial de outro vendedor, mesmo com o mesmo telefone. "
    "Preco, campanha, fidelidade, multa, bonus, cobertura, agenda e desconto precisam da fonte oficial "
    "vigente ou aprovacao explicita; exemplos historicos de excecao nao sao autorizacoes. "
    "Nao prometa ativacao amanha, em 24h ou 48h sem confirmacao efetiva da cobertura e agenda. "
    "CRM_REGISTRADO exige ID real retornado pelo CRM; credito exige resposta real da integracao; "
    "CONTRATO_ENVIADO exige emissao; ASSINATURA_CONFIRMADA exige verificacao do provedor; "
    "CONTRATO_ATIVO exige ativacao no sistema. 'Assinei', 'resumo da venda' e "
    "'atendimento bem-sucedido' sozinhos NAO comprovam assinatura ou venda. "
    "Nao trate silencio como autorizacao para aumentar descontos ou repetir mensagens. "
    "Respeite opt-out, decisor e data de retorno combinada; jamais force combo se escolheu so fibra. "
    "Pergunte no maximo uma coisa por vez. As chamadas de IA podem funcionar 24/7; "
    "a reativacao ativa deve respeitar o momento escolhido e as regras do canal. "
    "Para questoes de golpe ou documentos, explique o canal oficial e a finalidade sem pedir "
    "dados adicionais. Se o cliente pede humano ou precisa de uma excecao, nao invente uma "
    "autorizacao nem prometa contato sem ter realizado a transferencia autorizada."
)

_SIGNAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("STOP_CONTACT", re.compile(
        r"\b(?:parem|pare de mandar|parem de mandar|nao me (?:mandem|mand[a-z]*|contatem|liguem)|"
        r"nao quero receber|sem interesse|nao tenho interesse|"
        r"nao entre em contato|me descadastre|retire meu numero)\b"
    )),
    ("ONLY_FIBER", re.compile(
        r"\b(?:movel nao preciso|nao preciso (?:de )?(?:movel|celular)|"
        r"so (?:quero )?(?:internet|fibra)|apenas (?:internet|fibra)|"
        r"sem (?:o |plano )?movel|nao quero (?:o |plano )?movel)\b"
    )),
    ("CONDITIONAL_BUDGET", re.compile(
        r"\b(?:ate|no maximo|limite de)\s*(?:r\s*\$?\s*)?\d{2,4}(?:[,.]\d{1,2})?\s*(?:eu )?(?:fecho|contrato|pago)\b"
    )),
    ("SIGNATURE_ERROR", re.compile(
        r"\b(?:link nao abre|link nao funciona|erro (?:no |de )?link|"
        r"nao consegui (?:assinar|abrir)|assinatura (?:falhou|deu erro)|link expirou)\b"
    )),
    ("SIGNATURE_REPORTED", re.compile(
        r"\b(?:ja assinei|assinei|contrato assinado|terminei de assinar)\b"
    )),
    ("SECURITY_CONCERN", re.compile(
        r"\b(?:golpe|fraude|nao confio (?:no|nesse) link|"
        r"medo (?:de|do) golpe|link suspeito|e seguro (?:esse|o) link)\b"
    )),
    ("CREDIT_REJECTED", re.compile(
        r"\b(?:credito (?:reprovado|negado)|cpf (?:reprovado|negado)|"
        r"analise (?:reprovada|negada)|contestacao do credito)\b"
    )),
    ("URGENCY_INSTALL", re.compile(
        r"\b(?:instalar amanha|instalacao amanha|instala amanha|"
        r"instalar hoje|instalacao hoje|instalar em 24 horas|"
        r"consegue instalar amanha|preciso instalar urgente)\b"
    )),
    ("FIDELITY", re.compile(
        r"\b(?:fidelidade|multa|cancelamento antecipado|"
        r"vou ficar (?:so |apenas )?\d+ meses|"
        r"tenho (?:so |apenas )?\d+ meses (?:no|nesse) imovel)\b"
    )),
    ("DECISION_MAKER", re.compile(
        r"\b(?:meu esposo|minha esposa|meu marido|minha mulher|"
        r"meu diretor|minha diretora|meu gestor|"
        r"consultar (?:o |a )?(?:diretor|diretora|gestor|esposo|esposa))\b"
    )),
    ("PRICE", re.compile(
        r"\b(?:preco|caro|cara|mensalidade|orcamento|desconto|mais barato)\b"
    )),
    ("COMPETITOR", re.compile(
        r"\b(?:outra operadora|outro provedor|concorrente|trocar de operadora)\b"
    )),
    ("PURCHASE_INTENT", re.compile(
        r"\b(?:quero contratar|quero assinar|pode fechar|pode seguir|"
        r"manda o contrato|mandar contrato|vamos fechar|fechamos)\b"
    )),
    ("AUDIO_UNTRANSCRIBED", re.compile(
        r"\baudio nao transcrito\b"
    )),
    ("RETURN_COMMITMENT", re.compile(
        r"\b(?:respondo|retorno|volto|me chama|me chame|me liga|me ligue|"
        r"me procure|te aviso|aviso voce|dou resposta)\b"
    )),
)


def fold_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", plain.casefold().replace("áudio", "audio")).strip()


def classify_signal(value: str | None) -> str:
    folded = fold_text(value)
    for signal, pattern in _SIGNAL_RULES:
        if pattern.search(folded):
            return signal
    return "GENERAL"


_COACHING = {
    "STOP_CONTACT": (
        "Cliente recusou a prospeccao. Agradeca e encerre sem contorno de objecao; "
        "registre preferencia de nao contato e NAO programe follow-up."
    ),
    "ONLY_FIBER": (
        "Cliente escolheu somente fibra; prossiga com o plano de fibra aceito "
        "e nao condicione fechamento a movel, combo ou venda adicional."
    ),
    "CONDITIONAL_BUDGET": (
        "Aceite condicionado ao teto de preco. Compare apenas o catalogo elegivel e "
        "custo total; nao invente desconto ou condicao especial. Escale excecao quando permitido."
    ),
    "SIGNATURE_ERROR": (
        "Diagnostique o problema do link ou assinatura e consulte o estado real "
        "antes de reenviar; nunca afirme assinatura confirmada sem evento do provedor."
    ),
    "SIGNATURE_REPORTED": (
        "Cliente disse que assinou: confirme por integracao. Nao diga concluido "
        "ate o provedor confirmar; so depois avance para OS e ativacao."
    ),
    "SECURITY_CONCERN": (
        "O cliente teme golpe. Confirme apenas canais oficiais e finalidade do link "
        "ou documento, ofereca atendimento humano autorizado; nao minimize nem peca dados extras."
    ),
    "CREDIT_REJECTED": (
        "Credito reprovado precisa de retorno oficial. Nao afirme aprovacao; "
        "ofereca apenas contestacao ou alternativa que exista no fluxo aprovado."
    ),
    "URGENCY_INSTALL": (
        "Cliente precisa de instalacao urgente. Antes de prometer amanha/24h, "
        "consulte cobertura e disponibilidade de agenda/OS. Se nao houver, informe limitacao."
    ),
    "FIDELITY": (
        "Explique a fidelidade e multa apenas pela condicao contratual oficial "
        "e pelo cenario de mudanca do cliente; excecao so com aprovacao."
    ),
    "DECISION_MAKER": (
        "Respeite outro decisor: disponibilize resumo da proposta e confirme "
        "o horario de retorno preferido. Nao pressione nem antecipe o follow-up."
    ),
    "PRICE": (
        "Diagnostique o teto de mensalidade, compare valor atual e oferta elegivel; "
        "nao altere preco nem conceda meses gratis sem regra aprovada."
    ),
    "COMPETITOR": (
        "Pergunte o que precisa melhorar em relacao ao provedor atual e compare "
        "somente diferenciais comprovados; nao deprecie concorrente."
    ),
    "PURCHASE_INTENT": (
        "Sinal claro de compra: avance IMEDIATAMENTE ao fluxo de fechamento "
        "autorizado; nao reapresente catalogo nem reinicie diagnostico."
    ),
    "AUDIO_UNTRANSCRIBED": (
        "Audio sem transcricao: nao presuma o conteudo. Solicite descricao em texto "
        "ou use somente a ferramenta de transcricao autorizada."
    ),
    "RETURN_COMMITMENT": (
        "Cliente definiu que retornara: nao antecipe follow-up. Respeite dia/hora "
        "expressos e nao envie cobrancas repetidas."
    ),
    "GENERAL": (
        "Responda a necessidade atual e proponha o proximo passo executavel "
        "sem repetir perguntas respondidas."
    ),
}


def coaching_for_context(context: dict) -> str:
    """Nao consulta outros contatos nem copia texto ou dados pessoais ao prompt."""
    latest = str(context.get("latest_customer_message") or "")
    return f"[{PROTOCOL_VERSION}/{classify_signal(latest)}] {_COACHING[classify_signal(latest)]}"


@dataclass(frozen=True)
class SalesEvidence:
    """Eventos reais; texto do cliente 'assinei' nao vira signature_verified."""
    interest: bool = False
    diagnosed: bool = False
    proposal: bool = False
    objection: bool = False
    agreement: bool = False
    crm_opportunity_id: int | None = None
    credit_result_received: bool = False
    contract_sent_verified: bool = False
    signature_verified: bool = False
    installation_scheduled_verified: bool = False
    activation_verified: bool = False
    lost_confirmed: bool = False
    human_transfer_confirmed: bool = False


def stage_from_evidence(evidence: SalesEvidence) -> str:
    if evidence.activation_verified:
        return "CONTRATO_ATIVO"
    if evidence.lost_confirmed:
        return "PERDIDO"
    if evidence.human_transfer_confirmed:
        return "TRANSFERIDO_A_HUMANO"
    if evidence.installation_scheduled_verified:
        return "INSTALACAO_AGENDADA"
    if evidence.signature_verified:
        return "ASSINATURA_CONFIRMADA"
    if evidence.contract_sent_verified:
        return "CONTRATO_ENVIADO"
    if evidence.credit_result_received:
        return "CONSULTA_CREDITO"
    if evidence.crm_opportunity_id:
        return "CRM_REGISTRADO"
    if evidence.agreement:
        return "ACORDO_COMERCIAL"
    if evidence.objection:
        return "OBJECAO_PENDENTE"
    if evidence.proposal:
        return "PROPOSTA_APRESENTADA"
    if evidence.diagnosed:
        return "DIAGNOSTICO"
    if evidence.interest:
        return "INTERESSE_CONFIRMADO"
    return "NOVO_CONTATO"


_RETURN_ACTION = re.compile(
    r"\b(?:respondo|retorno|volto|me chama|me chame|me liga|me ligue|"
    r"pode me chamar|pode voltar|me procure|te aviso|aviso voce|"
    r"dou (?:uma )?resposta|te dou (?:uma )?resposta|"
    r"falo com voce|converso com voce)\b"
)
_DAYS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
_TIME = re.compile(
    r"\b(?:as|a partir das|pelas)\s*(\d{1,2})(?::(\d{2}))?\s*(?:h|horas)?\b"
)
_HOUR = re.compile(r"\b(\d{1,2})\s*h(?:\s*(\d{2}))?\b")


def requested_return_not_before(
    customer_message: str | None,
    received_at: datetime,
) -> datetime | None:
    """Traduz apenas compromisso EXPLICITO de retorno em horário mínimo (UTC).

    Sem NLP de data livre e sem inferir datas a partir de 'instala amanhã',
    período de contrato, preço ou falas OUTBOUND. Usa o timestamp da entrada,
    não o instante do worker, para estabilidade ao reiniciar.
    """
    folded = fold_text(customer_message)
    if not _RETURN_ACTION.search(folded):
        return None
    received_utc = (
        received_at.replace(tzinfo=timezone.utc)
        if received_at.tzinfo is None else received_at.astimezone(timezone.utc)
    )
    local = received_utc.astimezone(BRAZIL)
    delta_days = None

    if re.search(r"\bamanha\b", folded):
        delta_days = 1
    elif re.search(r"\bhoje\b", folded):
        delta_days = 0
    else:
        for weekday, label in enumerate(_DAYS):
            if re.search(rf"\b{label}(?: feira)?\b", folded):
                delta_days = (weekday - local.weekday()) % 7
                break

    time_match = _TIME.search(folded) or _HOUR.search(folded)
    explicit_hour = time_match is not None
    if explicit_hour:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")
        if hour > 23 or minute > 59:
            return None
    elif re.search(r"\b(?:manha|de manha|pela manha)\b", folded):
        hour, minute = 9, 0
    elif re.search(r"\b(?:tarde|a tarde|pela tarde)\b", folded):
        hour, minute = 15, 0
    elif re.search(r"\b(?:noite|a noite|pela noite)\b", folded):
        hour, minute = 19, 0
    else:
        hour, minute = 15, 0

    if delta_days is None and not explicit_hour:
        return None
    delta_days = delta_days or 0
    requested_local = (
        (local + timedelta(days=delta_days))
        .replace(hour=hour, minute=minute, second=0, microsecond=0)
    )
    if requested_local <= local:
        if re.search(r"\bhoje\b", folded):
            return None  # horário pedido já passou
        if re.search(r"\bamanha\b", folded):
            return None
        if any(re.search(rf"\b{x}(?: feira)?\b", folded) for x in _DAYS):
            requested_local += timedelta(days=7)
        else:
            requested_local += timedelta(days=1)
    return requested_local.astimezone(timezone.utc)
