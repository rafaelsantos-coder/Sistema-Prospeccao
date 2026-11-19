# Publicação segura dos workers — Sulnet Prospecta

Objetivo: **preservar atendimento, campanhas e telefonia** durante atualizações.
Este documento é um *runbook*, não uma autorização para atualizar a produção.

## Situação verificada em 19/11/2026 UTC

- `web`: duas réplicas em produção, healthcheck `/api/health`,
  overlap de 10 segundos e draining de 30 segundos.
- `campaign-worker`, `ai-sdr-worker`, `voice-test-worker` e
  `voice-engine-worker`: uma réplica cada, vinculadas à branch `main`.
- O motor de telefonia executa chamadas reais em processo singleton PJSUA2;
  uma implantação pode interromper uma chamada que já foi atendida.
- O Railway envia `SIGTERM` e, após o limite configurado,
  `SIGKILL`. **Sem draining adequado, o tratamento cooperativo de sinais não
  preserva a chamada ou o envio em andamento.**

Referências da plataforma:
- https://docs.railway.com/deployments/deployment-teardown#draining-time
- https://docs.railway.com/deployments/deployment-teardown#overlap-time
- https://docs.railway.com/deployments/reference#singleton-deploys

## O que o PR #216 implementa

- Captura `SIGTERM`/`SIGINT` e acorda imediatamente as esperas.
- Campanhas: não faz novos claims após sinal; rollback se ainda não começou
  a enviar; conclui transação de mensagem enviada antes da saída.
- VoIP: não inicia nova reserva após sinal; tenta terminar a chamada/teste
  já iniciado antes de encerrar voluntariamente.
- Testes sem envios externos comprovam o comportamento do sinal.

## O que ainda falta para garantir uma publicação sem interrupção

1. **Draining por worker:** definir um intervalo maior que o tempo máximo
   permitido para cada trabalho (a ser medido). Não alterar produção
   antes de testar essa configuração na homologação.
2. **Motor SIP singleton:** definir exclusão mútua/lease de liderança por
   conta ou instância, inclusive no período de troca de deployments.
   Não aumentar réplicas nem habilitar overlap concorrente sem fencing.
3. **Exatamente uma vez / idempotência:** validar no provedor de WhatsApp
   e no banco se uma resposta recebida após perda de conexão com o banco
   pode resultar em duplicação de mensagem. Quando não houver prova de
   envio, registrar estado de resultado desconhecido, sem reenviar
   automaticamente.
4. **Limite de chamada:** se uma chamada SIP puder durar mais do que
   o limite máximo viável de draining, publicar somente quando não houver
   chamadas ativas; nenhuma opção de deploy consegue preservar SIP em
   um processo que é forçadamente encerrado.
5. **IA e processos auxiliares:** avaliar encerramento dos jobs e dos
   threads em `ai-sdr-worker`. O PR #216 trata apenas dos três workers
   de campanhas, discagem e testes SIP.
6. **Teste de corte:** ensaiar `SIGTERM` e `SIGKILL` em homologação
   com banco de testes e destinatários internos, verificando reserva,
   status de envio, histórico de chamadas e retomada sem duplicações.

## Checklist obrigatório antes de publicar

- [ ] CI, CodeQL e análises das imagens aprovados no commit da entrega
- [ ] Staging com a mesma combinação de código e configuração a publicar
- [ ] Simulação de SIGTERM durante o processamento e testes de recuperação
- [ ] Provas de ausência de duplicação e de chamadas indevidas
- [ ] Garantia de que blacklist, limites e horário das campanhas permanecem
      vigentes mesmo depois do reinício
- [ ] Verificação de ausência de chamadas ativas ou draining suficiente
- [ ] Backup recente verificado e rollback documentado
- [ ] Confirmar saúde de login, API, webhooks, Z-API e SIP após mudança
- [ ] Registrar responsável, decisão e hora exata de publicação

**Se qualquer condição não puder ser demonstrada, NÃO publicar em produção.**
O fallback seguro é preparar a alteração e escolher uma janela sem chamadas e
sem campanhas ativas. Ter duas réplicas do web não assegura que os workers
singleton possam continuar uma ligação sem interrupção.
