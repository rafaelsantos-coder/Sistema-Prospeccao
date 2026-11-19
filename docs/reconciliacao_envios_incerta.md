# Entrega incerta nos disparos — segurança contra mensagens duplicadas

## Por que esta proteção existe

A Z-API (ou o provedor Telegram) é um sistema externo. Ele pode aceitar uma
mensagem e não conseguir devolver a confirmação ao Sulnet Prospecta.
Também é possível que o processo caia **depois** de obter a confirmação
externa, mas **antes** de salvar o resultado final no banco.

Reenviar automaticamente nessas situações pode contatar o cliente duas vezes.
A solução prioriza **não repetir automaticamente** um contato potencialmente
já enviado. Isso não é garantia de que toda mensagem será entregue.

## Fluxo implementado

1. O worker reserva um destinatário no banco: `PENDENTE → ENVIANDO`;
   incrementa `attempts` e **confirma a transação antes** da chamada externa.
2. O provedor responde com sucesso e o sistema finaliza em `ENVIADO`,
   gravando ID externo, histórico e atividade.
3. Em erro de comunicação/retorno incerto (incluindo timeout), a linha vai
   para `INDETERMINADO`; não volta para `PENDENTE`.
4. Se o processo morrer abruptamente após a reserva, `ENVIANDO` permanece
   gravado. Somente estados `PENDENTE` são selecionados por claims posteriores.
5. Cancelar um disparo converte apenas `PENDENTE` em `IGNORADO`;
   reabrir só recupera essas linhas canceladas. `ENVIANDO` e
   `INDETERMINADO` nunca são reenfileirados por essa operação.

## Reconciliação operacional (atualmente manual)

- Verifique na **instância e fila corretas** da Z-API, no WhatsApp enviado
  e nos eventos/webhooks, se o número recebeu a mensagem no horário da
  tentativa. Considere ID externo, tipo de mensagem e eventuais anexos.
- `INDETERMINADO` significa **não sabemos se foi entregue**. Não é sinônimo
  de erro definitivo, nem de mensagem recebida.
- `ENVIANDO` por período incompatível com o tamanho da operação pode
  indicar interrupção de processo; investigar o histórico antes de agir.
- Não editar status diretamente no banco nem criar um novo disparo para
  esses destinatários sem conferência documentada e autorização de gestão.
- Registrar evidências da consulta e decisão. Um fluxo futuro poderá
  automatizar conciliação por ID externo e eventos de confirmação, sem
  reenvios automáticos em cenários não confirmados.

## Limitações conhecidas

- Um crash **antes** de chamar a API pode deixar `ENVIANDO` sem envio real.
  O sistema evita duplicação automática, mas pode depender de revisão humana
  para recuperar contatos que jamais foram enviados.
- Campanhas com vários anexos podem ter parte da mídia entregue antes da
  interrupção. Não reenviar o pacote todo sem verificar cada item.
- Esta proteção não substitui exclusão mútua distribuída, idempotência do
  provedor (se disponível), nem prova de funcionamento após `SIGKILL`.
- O serviço de campanhas é **singleton** em produção. Não aumentar o número
  de réplicas para compensar sem validar transações, fencing e ausência de
  concorrência entre instâncias.
- O presente PR é somente código/testes. A publicação requer aprovação,
  homologação e plano de reversão dos workers. Não implantar junto a chamadas
  ativas sem prova de continuidade SIP.

Referências internas: issue #214 e PRs #217/#218.
