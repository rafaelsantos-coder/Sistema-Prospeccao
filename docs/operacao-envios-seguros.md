# Não duplicar disparos após falha — rotina operacional

> Implementação de segurança em branch isolada. Não autoriza reativar
> campanhas nem publicar workers em produção sem validação de homologação.

## Estados dos destinatários

| Estado | Significado | Pode reenviar automaticamente? |
| --- | --- | --- |
| PENDENTE | O sistema ainda não reservou a tentativa de envio | Somente quando a campanha está autorizada e ativa |
| ENVIANDO | Reserva persistida antes de acessar o provedor | **Não**; pode ter sido aceito antes de uma queda |
| INDETERMINADO | Resposta externa incerta (timeout, 429, 5xx, falha de rede etc.) | **Não** |
| ENVIADO | Provedor confirmou e resultado foi registrado no banco | Não |
| FALHOU | Recusa determinística ou problema interno | Somente conforme análise, sem disparo automático |
| IGNORADO | Bloqueio, cancelamento de pendente ou opção de não contato | Não |

O worker confirma **ENVIANDO e tentativas** no banco antes de enviar para
a Z-API/Telegram. Só depois persiste o resultado. Um reinício entre esses
passos nunca repõe o destinatário na fila. Isso privilegia **at-most-once
automático** e não garante entrega; quem caiu antes do HTTP pode permanecer
em ENVIANDO sem envio externo.

## Conferência gerencial

1. Identificar a campanha, fila e **instância correta** da Z-API.
2. Consultar o histórico de envio no WhatsApp/provedor; comparar número,
   horário, corpo da mensagem, botões, anexos e ID de mensagem.
3. ENVIANDO há muito tempo indica possível queda do trabalhador; não
   significa que o cliente recebeu ou que a mensagem falhou.
4. INDETERMINADO exige conciliação. **Não criar campanha nova com os mesmos
   contatos** nem editar status diretamente no banco para forçar reenvio.
5. Em campanha com mídia, texto e anexos podem ter sido entregues
   parcialmente. Avaliar cada item antes de qualquer ação.
6. Respeitar a blacklist global mesmo durante recuperação.

## Rejeições conhecidas e botão de recuperação

- **HTTP 401/403:** a Z-API recusou autenticação/autorização.
  O disparo é pausado, o destinatário volta para PENDENTE sem consumir
  tentativas; o gestor deve corrigir a credencial e revisar antes de retomar.
- **HTTP 400/404/422:** o endpoint recusou o envio. O sistema guarda uma
  marca de recusa determinística; o gestor pode preparar esses contatos
  individualmente pelo fluxo de falhas somente com a campanha pausada
  e confirmação explícita. Isso não inicia envio imediato.
- **HTTP 429, 5xx, timeout ou erro de rede:** a resposta pode ser incerta,
  portanto o status fica INDETERMINADO e não há retry.
- **Histórico antigo FALHOU com mensagem genérica** não possui prova
  suficiente de recusa: o botão de recuperação não o recoloca na fila.

## Encerramento de worker

Em SIGTERM/SIGINT o worker para novos claims e aguarda a operação em
andamento. Se a solicitação chegar após reservar mas antes do commit,
faz rollback da reserva. O tempo de draining do orquestrador deve ser
suficiente para a chamada ao provedor e o commit final. SIGKILL não
preserva o processo, mas a reserva persistida evita replay automático.

**Limites:** Nenhum dos itens implementa confirmação exatamente uma vez,
controle global de 30 segundos entre todos os tipos de campanha/manual,
nem proteção completa de chamadas SIP durante encerramento forçado.
Esses pontos exigem ensaios separados e não estão liberados para deploy
produtivo por este documento.

Referências: issue #214 e PRs #218/#220.
