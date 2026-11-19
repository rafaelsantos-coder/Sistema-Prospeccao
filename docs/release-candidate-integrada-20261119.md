# Release candidate integrada — Sulnet Prospecta (19/11/2026)

**Situação:** branch de homologação, sem merge ou deploy automático em produção.

## Funcionalidades presentes

1. **WhatsApp seguro:** reserva ENVIANDO/attempts confirmada antes de chamar
   Z-API; respostas incertas em INDETERMINADO sem novo envio; recusa
   determinística tratada separadamente; cancelamento não atinge chamadas
   externas já iniciadas; SIGTERM cooperativo do worker.
2. **Pacing manual existente:** mantido o agendamento por instância de
   30 segundos para primeiro contato, sem reativar campanhas pausadas.
3. **Outbox manual confiável:** timeout/resultado ambíguo marcado UNKNOWN,
   ícone de aviso em vez de confirmação falsa, e reservas PROCESSING com
   mais de 15 minutos recuperadas como UNKNOWN. O contato antigo nunca
   volta automaticamente para PENDING; a instância pode atender novos contatos.
4. **VoIP seguro:** liderança global exclusiva e bloqueio por conta SIP
   compartilhado entre discador e agente diagnóstico. Falha de sessão
   invalida lock/conexão. SIGTERM drena chamada em andamento.
5. **Conciliação gerencial:** tela SOMENTE DE LEITURA em
   `/dispatches/reconciliation`, acessível com
   `dispatches.reconcile` (ADMIN/GESTOR), com filtros de status/origem,
   contagem de envios incertos e marcação de processamento longo.
6. **Testes automatizados** incluem ACK externo + rollback, interrupções
   forçadas simuladas, cancelamento/reabertura, permissão granular,
   disputa de locks no PostgreSQL e PBX SIP local falso.

## Critérios de liberação

- [ ] CI combinada aprovada: pytest, PostgreSQL, CodeQL, segredos, Trivy
- [ ] Sem alterações destrutivas, pendências de migração ou troca de credenciais
- [ ] Staging livre de alterações pendentes de outros trabalhos
- [ ] Testar web + healthcheck em staging (sem envios reais)
- [ ] Ambiente SIP isolado com contas e ramais de teste e janela autorizada
- [ ] Plano de draining e reversão para campanhas e telefonia
- [ ] Aprovação explícita da alteração dos workers de produção

## Pontos que NÃO devem ser inferidos

- Um SIGKILL pode derrubar uma chamada SIP ativa.
- ENVIANDO e INDETERMINADO não significam entregue nem não entregue.
- Não há garantia de exactly-once com Z-API.
- O limite manual de primeiro contato por instância não garante
  30 segundos globais em TODAS as campanhas e respostas do sistema.
- Painel de conciliação não permite reenvios; qualquer ação exige
  verificação no provedor, autorização e respeitar a blacklist global.
- A IA SDR automatizada permanece desativada por decisão operacional;
  esta release não altera essa política.

Referências: PRs #240, #242 e #232; issue #214.
