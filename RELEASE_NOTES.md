# Sulnet Prospecta v0.2.0

Versão de homologação da integração WhatsApp pela Z-API.

## Incluído nesta versão

- Consulta segura do status da instância Z-API.
- Envio de mensagem interna de teste pela tela de Configurações.
- Envio individual de WhatsApp a partir do detalhe do lead.
- Registro automático das mensagens enviadas no histórico do CRM.
- Webhook para mensagens recebidas e atualização de status.
- Associação de respostas ao lead por telefone.
- Interrupção da próxima ação quando o cliente responder.
- Detecção de pedido de descadastro e bloqueio como `Não contatar`.
- Registro de auditoria para envio, recebimento e configuração de webhook.
- Credenciais armazenadas somente em variáveis do Railway.
- Campanhas em massa mantidas bloqueadas durante a fase piloto.

## Infraestrutura

- Aplicação: serviço `web` no Railway.
- Banco: PostgreSQL persistente.
- Domínio: `https://sulnet-prospecta.up.railway.app`.
- Healthcheck: `/api/health`.

Nenhuma credencial, chave de API ou arquivo `.env` está incluído no repositório.
