# Checklist de ativação — Vendedor IA

Homologar em staging antes de qualquer ambiente com clientes reais. Ligar o
perfil `CLOSER` no painel **não** equivale a operação homologada.

Ver também: [`vendedor-ia-fechamento.md`](vendedor-ia-fechamento.md).

## Pré-requisitos

- [ ] `OPENAI_API_KEY` configurada no web e no worker
- [ ] `SULNET_CRM_BASE_URL` apontando para o CRM de homologação
- [ ] `SULNET_AI_INTEGRATION_KEY` (ou bearer legado) com ≥ 24 caracteres
- [ ] `ZAPSIGN_WEBHOOK_SECRET` com ≥ 24 caracteres e rota
  `/webhooks/zapsign/<segredo>` registrada na ZapSign
- [ ] Worker `ai_sdr_worker` rodando com as mesmas variáveis do web
- [ ] Instância Z-API da fila de fechamento com webhook ativo
- [ ] Perfil CLOSER criado, `active=true`, `execution_mode=AUTO` **somente após**
  os testes abaixo

## Flags (ordem)

1. Manter `AI_SELLER_ENABLED=false` enquanto valida CRM e ZapSign offline.
2. Em staging: `AI_SELLER_ENABLED=true` + `AI_AUTONOMOUS_ENABLED=true`.
3. Em produção o boot **recusa** seller sem autonomia, OpenAI, CRM e ZapSign.
4. Só então colocar o agente CLOSER em `AUTO` no painel `/ai/seller`.

## Casos mínimos de aceite

| # | Cenário | Resultado esperado |
|---|---------|-------------------|
| 1 | SDR confirma compra em oferta válida | Handoff para CLOSER; SDR para de responder |
| 2 | Cliente já informou nome/CPF/cidade no SDR | Closer não pergunta de novo |
| 3 | Crédito aprovado | Segue coleta e gera contrato |
| 4 | Crédito reprovado | Oferece outro CPF ou encerra; sem contrato |
| 5 | Assinatura ZapSign concluída | `contract_status` atualiza; oportunidade acompanha |
| 6 | Timeout / CRM fora | Erro auditado; sem venda marcada como ganho |
| 7 | Cliente pede pessoa no meio do fechamento | Escala humano; cancela follow-ups do closer |
| 8 | Cliente atual pedindo troca de plano | Redireciona ao telefone de atendimento; não cria venda nova |

## Segurança

- [ ] CPF e documentos não aparecem em logs de IA sem redação
- [ ] `AI_SUPERVISOR_GUARDIAN_SHADOW_ENABLED=true` por pelo menos uma semana nos
  runs do closer (auditoria `AI_SUPERVISOR_GUARDIAN_REVIEW` com `shadow_only`)
- [ ] Taxa de `would_intervene` e falsos positivos revisada antes do enforce
- [ ] Só então `AI_SUPERVISOR_GUARDIAN_ENABLED=true` (enforce)
- [ ] Opt-out e `NAO_CONTATAR` interrompem o fechamento
- [ ] Rollback documentado: `AI_SELLER_ENABLED=false` e CLOSER em `SHADOW`

## Rollback rápido

```text
AI_SELLER_ENABLED=false
AI_AUTONOMOUS_ENABLED=false
```

No painel, colocar o CLOSER em `SHADOW` ou `active=false`. Reiniciar o worker.
