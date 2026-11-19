# Contrato CRM Comercial / Sulnet Prospecta — Contestação de Crédito

ESTADO 19/11/2026: a outra equipe confirmou os quatro webhooks COMERCIAIS publicados no servidor 204. Smoke de contest-reasons retornou 4 motivos e a autenticação rejeitou chave ausente/inválida com HTTP 401. Os outros endpoints ainda não tiveram teste ponta a ponta com o Prospecta neste PR; não executar contestação real nem Serasa para homologar. Feature flag: SULNET_CRM_CONTESTATION_ENABLED=false por padrão, a ser habilitada no Railway após CI e deploy validado.

## Segurança comum

Todos os endpoints abaixo utilizam X-Integration-Key, a mesma chave comercial de POST /webhook/leads. Não usar o Bearer do SDR IA.

Validar em todas as requisições que sellerUsername corresponde a usuário ativo vendedor/supervisao, assigned_user_id da oportunidade, e analysisId pertence à mesma oportunidade. Responder 401/403/404 sem expor registros de outros usuários. Nunca gerar nova oportunidade, atualizar funil/etapa, executar nova consulta Serasa ou reativar IA.

A UI do Prospecta limita o envio ao responsável atual pela conversa. Permissão de contestar, critérios, motivo, anexos e decisão devem continuar CENTRALIZADOS no CRM: creditAnalysisAllowsContest, creditGetContestReasons, creditSyncOpportunityStatus, histórico CREDIT_CONTESTED. A decisão é feita exclusivamente na UI gerencial/supervisão do CRM.

Todos os webhooks retornam opportunityId, analysisId, sellerUsername, assignedUserId e funnelId reais, para validar identidade do vínculo.

## 1. Status read-only

POST /webhook/leads/credit-status
JSON:
{"opportunityId":16976,"analysisId":3501,"sellerUsername":"fabiano.braga"}

Resposta exemplo 200:
{"opportunityId":16976,"analysisId":3501,"sellerUsername":"fabiano.braga","assignedUserId":36,"funnelId":17,"status":"Reprovado","result":"Reprovado","canContest":true,"contestationAt":null,"managementDecision":null}

canContest precisa ser booleano e derivado exclusivamente do CRM, jamais inferido do texto local. Retornar a decisão gerencial atual ou null. Não consultar Serasa. O Prospecta fará leitura a cada 2 minutos somente enquanto a contestação estiver pendente e a conversa estiver aberta.

## 2. Catálogo efetivo de motivos

POST /webhook/leads/contest-reasons, com JSON de IDs e vendedor idêntico ao anterior.

Resposta 200:
{
  "opportunityId":16976,"analysisId":3501,
  "sellerUsername":"fabiano.braga","assignedUserId":36,"funnelId":17,
  "reasons":[
    {"key":"geral","label":"Outro motivo","active":true,"requiresJustification":true,"requiresDocConfirmation":false,"minAttachments":0,"maxAttachments":0,"attachmentDocType":""},
    {"key":"provedor_atual","label":"Cliente já possui provedor","active":true,"requiresJustification":false,"requiresDocConfirmation":true,"minAttachments":1,"maxAttachments":1,"attachmentDocType":"contestacao_fatura"},
    {"key":"liberado_comprovante_provedor","label":"Liberado comprovante provedor","active":true,"requiresJustification":false,"requiresDocConfirmation":false,"minAttachments":0,"maxAttachments":3,"attachmentDocType":"contestacao_anexo"},
    {"key":"cliente_da_base_paga_em_dia","label":"Cliente da base paga em dia","active":true,"requiresJustification":true,"requiresDocConfirmation":false,"minAttachments":0,"maxAttachments":0,"attachmentDocType":""}
  ]
}

Motivos têm key STRING, não ID numérico, e devem vir de app_settings.credit_contestation_reasons + defaults. Metadados exigidos para qualquer motivo futuro: key, label, active, requiresJustification, requiresDocConfirmation, minAttachments, maxAttachments, attachmentDocType.

## 3. Upload S2S

POST /webhook/leads/contest-attachment, multipart/form-data:
- opportunityId, analysisId, sellerUsername, docType, file.
- docType: contestacao_fatura ou contestacao_anexo.
- Validar tipo PDF, JPG, PNG, MIME, assinatura, antivírus, limite de 10 MB por arquivo e ownership.
- Salvar usando mecanismo de uploads já existente, associado a entityType=analysis e entityId=analysisId.
- Nomes aleatórios dos arquivos no Prospecta, sem CPF no nome nem logs.

Resposta 200:
{"opportunityId":16976,"analysisId":3501,"sellerUsername":"fabiano.braga","assignedUserId":36,"funnelId":17,"attachmentId":4321}

## 4. Enviar contestação

POST /webhook/leads/contest

JSON:
{"opportunityId":16976,"analysisId":3501,"sellerUsername":"fabiano.braga","reasonType":"provedor_atual","justification":"","docConfirmed":true,"attachmentIds":[4321]}

Resposta 200:
{"opportunityId":16976,"analysisId":3501,"sellerUsername":"fabiano.braga","assignedUserId":36,"funnelId":17,"status":"Contestação enviada","canContest":false}

Validar todas as condições novamente no servidor e que cada anexo pertence à MESMA análise. Reutilizar o código compartilhado de PATCH /api/credit/:id/contest; não duplicar as regras de contestação na integração. Justificativa automática de provedor_atual se vazia. Fatura exige 1 anexo e checkbox; comprovante permite até 3 anexos opcionais; demais motivos conforme settings.

Após aceitar: status Contestação enviada, limpa management_*, sincroniza opportunities.credit_status, grava histórico e activity_log. Nova contestação bloqueada enquanto pendente, mas permitida quando o CRM indicar canContest=true após decisão desfavorável ou liberação condicional. Contestação comum nunca faz nova consulta Serasa.

## Comportamento no Prospecta

- Nome/telefone já registrados por POST /webhook/leads.
- CPF já consultado por POST /webhook/leads/consult, que devolve analysisId.
- Apresentar Contestar análise somente se API de status devolver canContest=true.
- Listar motivos DINAMICAMENTE; aceitar justificativa e documentos segundo o motivo.
- Não armazenar CPF, justificativa ou bytes dos anexos localmente, apenas IDs/status na auditoria.
- Mostrar Contestação enviada, decisão gerencial e status atualizado.
- Em timeout/erro 5xx, não reenviar automaticamente.
- Não chamar PATCH /api/credit/:id/manage, não alterar Kanban/etapa.
- Acesso de supervisores a oportunidades de outros vendedores requer contrato adicional para identificar corretamente o ator; fase atual só permite o responsável pela conversa.

## Critérios de aceite para a outra IA do CRM

1. Publicar os quatro endpoints no servidor 204.
2. Validar owner e análise dos 4 logins: maria.peno, claudia.linde, natine.hirt, fabiano.braga.
3. Garantir API JWT e fluxos atuais de decisão gerencial intactos.
4. Validar mocks e dry-run sem contestação real nem Serasa adicional.
5. Entregar payloads e respostas reais de homologação, inclusive canContest=true/false, fatura, até 3 anexos e recontestação.
6. Confirmar que a feature flag só será habilitada no Railway depois de testes do contrato.

## Atualização de contrato

O webhook /webhook/leads/consult passou a devolver canContest, contestation, managementDecision e billMonthExpected*. A interface utiliza exclusivamente a consulta read-only /webhook/leads/credit-status para confirmar a elegibilidade, evitando reconsulta ao Serasa. O endpoint /webhook/leads/contest-reasons fornece a competência esperada da fatura; o Prospecta apenas a exibe se presente no retorno (billMonthExpected ou billMonthExpectedLabel), sem inferir um mês novo. As operações seguem a mesma X-Integration-Key comercial de /webhook/leads.
