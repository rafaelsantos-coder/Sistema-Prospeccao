# Vendedor IA — Sulnet Prospecta

## Escopo implementado nesta primeira etapa

- Menu IA com itens **SDR IA** e **Vendedor IA**.
- Perfil de vendedor de fechamento com `role_type=CLOSER`, vinculado a usuário CRM comercial dedicado.
- Usuário técnico `sulia.vendedor@system.local`, tipo `CONSULTOR`, equipe `DIGITAL`, inativo e senha aleatória não exposta.
- Perfil IA de fechamento criado via seed, inicialmente inativo e em `SHADOW` para evitar disparos não homologados.
- Painel `/ai/seller` restrito a ADMIN/GESTOR: identidade, objetivo, modelo, instruções, histórico e base de conhecimento.
- O painel original `/ai` permanece dedicado ao SDR; não permite vincular o closer indevidamente a uma fila SDR.

## Comportamento esperado do próximo ciclo (ainda não automatizado)

1. SDR confirma intenção explícita e transfere conversa com histórico integral e resumo estruturado.
2. Vendedor IA identifica o que já consta do histórico: nome, telefone, CPF, plano, cidade, endereço e objeções.
3. Inicia oportunidade CRM (se ainda não existir), mantém seu ID e protocolo.
4. Solicita apenas dados faltantes: nome, telefone, CPF, CEP, rua, número/complemento, plano, e-mail e faturamento.
5. Consulta crédito/Serasa pelo CRM e informa apenas o resultado real.
6. Resolve endereço via CEP e valida dados obrigatórios.
7. Gera contrato e monitora assinatura por webhook.
8. Atualiza oportunidade e registra ganho somente após confirmação do CRM.
9. Dispensa campos operacionais de campo no funil SDR IA: NAP/ANATEL, coordenadas, técnico, agendamento manual, fotos e selfie.
10. Promete ativação em até 48 horas somente após validação de política e viabilidade, sem criar agendamento fictício.

## Integrações pendentes de homologação

O sistema Sulnet Prospecta deste repositório **não é** o CRM externo citado na especificação `docs/sdr-ia-campos-api.md`. Antes de habilitar o Vendedor IA real, confirmar host, credenciais em variáveis de ambiente e schema de:

- criação/atualização de oportunidade;
- `POST /api/capture/leads/{id}/consult` (crédito);
- `GET /api/capture/cep/{cep}` (endereço);
- geração de PDF, assinatura digital e `/webhook/contrato`;
- tratamento do status `reprovado`, falhas e escalonamento humano.

Não persistir CPF em texto livre nem logs de IA. Proteger dados pessoais, implementar timeout e idempotência, auditoria por agente, autorização por fila e testes automatizados.

## Segurança de ativação

Este PR **não** liga o novo agente à Z-API nem cria pedidos reais. A habilitação operacional exige implementar e testar handoff e CRM, e controlar explicitamente ações de cobrança, contrato e assinatura. Não confundir perfil de IA ativo com operação homologada.

Checklist operacional atualizado: [`VENDEDOR-IA-ATIVACAO.md`](VENDEDOR-IA-ATIVACAO.md).
