# Painel operacional gerencial

## Problema

A operação WhatsApp (filas humanas + Fila IA + fechamento) já gera muitos sinais — inbox, relatório histórico, qualidade IA, alertas no telefone do responsável — mas o gestor não tem **um único lugar em tempo quase real** para responder:

- Quantas conversas estão abertas / iniciadas hoje?
- Quantas falharam na IA?
- Quantas fecharam venda?
- Quantas pararam sem fechar?
- Quantos vendedores estão interagindo?
- Em que nível/estágio está cada conversa crítica?

## O que o mercado recomenda

Referências de contact center / WhatsApp CRM / conversational AI (NICE, Dynamics Wallboard, Infobip, Sprinklr, Kasumi, LandinChat):

1. **Wallboard ao vivo** — fila, espera, SLA, ocupação, abandonos.
2. **Funil de conversão** — iniciado → qualificado → proposta → contrato → ganho, com taxa por etapa.
3. **Containment da IA** — resolvido só pela bot vs handoff humano.
4. **Alertas acionáveis** — limiares (espera, silêncio, falha, risco de perda), não vanity metrics.
5. **Drill-down** — do KPI para a conversa em um clique.

## O que já existia no Prospecta

| Fonte | Serve para | Limitação |
|---|---|---|
| `/dashboard` | Leads/MRR CRM | Não olha filas WhatsApp |
| `/reports/whatsapp` | Histórico TME/TMR, jornada IA | Período fechado, não wallboard |
| `/conversations` | Trabalho do dia | Por operador, não KPIs gerenciais |
| `/conversations-intelligence` | Temperatura comercial | Sem falhas/vendas/SLA |
| `/ai/quality` + `/ai/weekly` | Qualidade e causas | Só recorte IA |
| `ops_alerts` | Avisos no WhatsApp | Sai do produto |

## Solução (entregue)

Rota **`/operations`** (+ `GET /api/operations/wallboard`) para ADMIN / GESTOR / SUPERVISOR.

### Bloco A — Agora (KPIs vivos)

- Abertas, iniciadas hoje, aguardando resposta, sem responsável
- Com IA / com humano
- Vendas assinadas hoje
- Paradas sem venda (erro CRM, silêncio no fechamento, tags de risco)
- Falhas de job IA sem resposta
- Vendedores online / total

### Bloco B — Funil do dia

Atendidos → contratação → contrato → venda · crédito reprovado · opt-out  
(reusa a lógica de `ai_daily_funnel` / marcador `_won_after_signature`)

### Bloco C — Filas e equipe

Tabela por fila (abertas, espera, pendentes, IA, online) e frota de vendedores (presença + carga).

### Bloco D — Alertas + atenção

Severidade CRITICAL / WARNING / INFO e lista clicável de conversas que pedem ação.

Atualização automática a cada ~20s via API JSON.

## Roadmap sugerido (próximas iterações)

1. **SLA configurável** — 1ª resposta em X minutos por fila; % dentro do SLA no wallboard.
2. **Containment rate** — % resolvido só pela IA vs handoff.
3. **Nível da conversa** — estágio SDR + passo de fechamento + tag comercial na lista de atenção.
4. **Feed in-app** dos `OPS_ALERT` (hoje só no telefone).
5. **Comparativo D-1 / semana** nos KPIs (tendência, não só valor absoluto).
6. **TV mode** — tela cheia para sala de operações.

## Princípios

- Um número, um dono, um limiar, uma ação.
- Preferir consolidar sinais existentes a criar modelos novos.
- Histórico continua em Relatórios WhatsApp; o wallboard é o “agora”.
