# Contrato proposto — CPF manual na oportunidade CRM

> **Estado:** endpoint informado como publicado no CRM Comercial, com validação dry-run sem consulta nova à Serasa (relatório recebido em 18/11/2026). Os testes automatizados do Prospecta não executam consulta real. A ativação no Railway depende de deploy e configuração do serviço.

## Objetivo

Após `POST /webhook/leads` cadastrar **nome + telefone**, permitir a consulta do CPF na **mesma oportunidade** e no **mesmo vendedor**. O usuário não sai da tela de Conversas.

## Endpoint disponível no CRM (conforme relatório da integração)

```http
POST /webhook/leads/consult
X-Integration-Key: <LEAD_INTEGRATION_API_KEY>
Content-Type: application/json

{
  "opportunityId": 17000,
  "sellerUsername": "maria.peno",
  "clientCpf": "11144477735"
}
```

Requisitos do endpoint:

- Autenticar exclusivamente por `X-Integration-Key` (integração servidor a servidor), sem personificar `sdr.ia`.
- Validar `opportunityId` no CRM, existência e acesso; checar que o usuário `sellerUsername` está ativo e é o dono (`assigned_user_id`) dessa oportunidade. Não aceitar outro vendedor indicado livremente.
- Não criar nova oportunidade. Gravar CPF na oportunidade existente e fazer/reutilizar a análise de crédito segundo as regras atuais do CRM.
- Respeitar as regras existentes de reutilização, consulta, análise de crédito, limites e auditoria do CRM.
- Devolver resultado real; nunca presumir aprovação quando ainda está pendente.
- Registrar a solicitação e resultado com controle de autorização e sem CPF em logs.
- Retornar erros 400 (CPF inválido), 401/403 (integração/autorização), 404 (oportunidade ausente), 409 (identidade/vínculo divergente) de forma estável.

## Resposta de sucesso esperada

```json
{
  "opportunityId": 17000,
  "assignedUserId": 24,
  "sellerUsername": "maria.peno",
  "funnelId": 16,
  "analysisId": 98765,
  "protocol": "CRM-2026-17000",
  "analysisProtocol": "SAC-2026-98765",
  "result": "Aprovado",
  "status": "Aprovado",
  "outcome": "reused",
  "score": 350,
  "approved": true,
  "rejected": false
}
```

Para `Aprovado com taxa`, devolver `status` com essa descrição exata. Para `Reprovado`, `rejected=true`. Para decisão pendente, `status="Em análise"`, `approved=false`, `rejected=false`.

## Prospecta — controles disponíveis

- Usuários Maria (`maria.peno`, CRM 24), Claudia (`claudia.linde`, 37), Natine (`natine.hirt`, 25), Fabiano (`fabiano.braga`, 36).
- Formulário CPF visível **somente depois do registro** CRM da conversa no vendedor responsável.
- CPF enviado em POST server-to-server; não é incluído em query, nem audit log, nem mensagem WhatsApp.
- Validação de CPF com dígitos verificadores.
- A consulta segue a oportunidade vinculada ao contato; não cria uma nova.
- Em caso de timeout, retorno incompleto ou proprietário divergente, o Prospecta bloqueia reenvio automático e pede conferência.
- O formulário fica **desabilitado** até definir `SULNET_CRM_MANUAL_CREDIT_ENABLED=true` no serviço web do Railway, após o merge e testes. Não são realizadas consultas de CPF automaticamente.
- O CPF usa **exatamente a mesma chave de `POST /webhook/leads`**, enviada exclusivamente em `X-Integration-Key`. O Prospecta utiliza `SULNET_CRM_LEAD_INTEGRATION_KEY` quando configurada ou a chave de integração atual `SULNET_AI_INTEGRATION_KEY` que já autentica o webhook comercial; **nunca utiliza o Bearer personificado da IA**.
