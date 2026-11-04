# Arquitetura do Sulnet Prospecta

## Componentes

```text
Navegador
   │
   ▼
FastAPI + Jinja (aplicação web)
   ├── Autenticação (senha, MFA, OIDC opcional)
   ├── CRM e funil comercial
   ├── Inbox WhatsApp e filas de atendimento
   ├── Campanhas, disparos e prospecção Apify
   ├── Painéis SulIA (SDR, closer, qualidade, conhecimento)
   └── Auditoria e relatórios
   │
   ▼
SQLAlchemy + Alembic
   ├── SQLite (desenvolvimento)
   └── PostgreSQL (produção)

Processo separado: ai_sdr_worker
   ├── Jobs SDR / Vendedor IA (tabela ai_jobs)
   ├── Follow-ups comerciais
   ├── Supervisor sombra, analista semanal
   └── Autocorretor / aprendizado / engenheiro (flags)

Integrações
   ├── Z-API (WhatsApp, multi-instância)
   ├── OpenAI (SulIA)
   ├── CRM Sulnet (crédito, oportunidade, contrato)
   ├── ZapSign (assinatura)
   ├── Apify / Google Maps
   ├── Resend / E-mail
   ├── S3-compatível (mídia e backup)
   └── Redis opcional (sessão server-side)
```

## Escopo de acesso

- **Administrador:** todas as unidades, usuários, integrações e configurações.
- **Gestor:** todas as unidades, campanhas e indicadores.
- **Supervisor:** dados da própria unidade.
- **Consultor:** leads atribuídos e atividades próprias.
- **Agente IA · SDR:** usuário técnico da fila; não opera o painel humano.

## Funil comercial

Novo → Tentativa de contato → Contato realizado → Qualificado → Proposta → Negociação → Análise de crédito → Viabilidade → Contrato → BKO/Auditoria → Agendamento → Instalação → Ganho.

Perdido e Não contatar são saídas controladas. “Não contatar” também bloqueia campanhas.

## Origem protegida

`TRAFEGO_PAGO` permanece como origem específica, ganha identificação visual e não participa da redistribuição automática. Isso evita misturar leads comprados com prospecção ativa, base ativa ou indicações.

## Fluxo WhatsApp → IA

1. Webhook Z-API autentica pelo segredo no path.
2. O motor de conversa resolve contato, lead, fila e responsável.
3. Se a fila está em `AI_FIRST` e nenhum humano atende, nasce um `ai_job`.
4. O worker aplica o SDR (ou o Vendedor IA após handoff) e responde pela mesma instância.
5. Pedido de pessoa escala para consultor; intenção de compra vai ao closer.

Ativação do Vendedor IA: [`VENDEDOR-IA-ATIVACAO.md`](VENDEDOR-IA-ATIVACAO.md).

## Evolução técnica em aberto

- Fila com broker (Redis/RQ ou equivalente) para jobs de IA e disparos.
- Guardião de saída em bloqueio após período em sombra.
- Cobertura/viabilidade geográfica integrada (hoje parcial via catálogo/cidades).
- Redistribuição automática de leads parados, excluindo tráfego pago.
- Painel unificado de latência e conversão da jornada IA.
