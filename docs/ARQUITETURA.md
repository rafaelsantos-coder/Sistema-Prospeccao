# Arquitetura do Sulnet Prospecta

## Componentes

```text
Navegador
   │
   ▼
FastAPI + Jinja (aplicação web)
   ├── Autenticação e perfis
   ├── CRM e funil
   ├── Atividades e follow-ups
   ├── Metas e indicadores
   ├── Campanhas e aprovação
   └── Auditoria
   │
   ▼
SQLAlchemy
   ├── SQLite (desenvolvimento)
   └── PostgreSQL (produção)

Integrações
   ├── Apify / Google Maps
   ├── Evolution API / WhatsApp
   ├── Resend / E-mail
   └── Scripts legados do repositório
```

## Escopo de acesso

- **Administrador:** todas as unidades, usuários, integrações e configurações.
- **Gestor:** todas as unidades, campanhas e indicadores.
- **Supervisor:** dados da própria unidade.
- **Consultor:** leads atribuídos e atividades próprias.

## Funil comercial

Novo → Tentativa de contato → Contato realizado → Qualificado → Proposta → Negociação → Análise de crédito → Viabilidade → Contrato → BKO/Auditoria → Agendamento → Instalação → Ganho.

Perdido e Não contatar são saídas controladas. “Não contatar” também bloqueia campanhas.

## Origem protegida

`TRAFEGO_PAGO` permanece como origem específica, ganha identificação visual e não participa da redistribuição automática. Isso evita misturar leads comprados com prospecção ativa, base ativa ou indicações.

## Próxima evolução técnica

- Alembic para migrações versionadas.
- Redis + worker para buscas e disparos duráveis.
- Webhooks de resposta da Evolution API e Resend.
- Integração com Voalle/ConectAI e verificação de cliente ativo.
- SSO corporativo.
- Cobertura/viabilidade geográfica integrada.
- Painel de qualidade, SLA e redistribuição por inatividade.
