# SulIA — protocolo comercial e avaliação (23/11/2026)

## Material de origem

- Relatório `Relatorio_Analise_Conversas_Fechamento_SulIA_23-11-2026.pdf`, exportação de 28/10 a 23/11/2026.
- Protocolo `Prompt_Treinamento_SulIA_Comercial_23-11-2026.md`.
- 36.186 mensagens, 1.833 protocolos, 1.418 contatos distintos; 137 protocolos com marcador de resumo e 43 com marcador mais relato textual de assinatura. **Nenhum desses totais equivale a contratos assinados, ativados ou faturados.**
- Há mensagens de áudio e imagens sem conteúdo transcrito. Não usar como evidência do argumento ou resultado.
- Protocolos referenciados sem nomes e telefones: 2026110600068, 2026111201031, 2026111600952, 2026111500163, 2026111001503, 2026111100859, 2026110400660 (padrões positivos); 2026110601075, 2026111400130, 2026110501333 (riscos).

## Regras vigentes nesta implementação

- `app/services/ai_sales_protocol.py` fornece classificador local de sinal e orientação curta ao SDR e ao gerador de follow-up.
- As instruções **não** copiam conversas, preços excepcionais nem dados pessoais para treinamento; o modelo recebe apenas um código de intenção e a regra pertinente.
- Uma dúvida deve ser respondida antes de retomar o script. Compra explícita exige avanço. Recusa expressa exige respeito e parada da prospecção. Apenas fibra não deve obrigar combo.
- Prazo de instalação, desconto, carência, isenção, crédito, multa, disponibilidade e assinatura exigem fonte oficial ou confirmação do sistema. Protocolo não autoriza exceções.
- `requested_return_not_before` transforma somente um pedido explícito de retorno com dia e/ou hora em horário UTC. O worker respeita esse horário ao agendar e imediatamente antes de enviar; não altera a possibilidade de a IA responder 24/7 a novas mensagens.
- A evidência de estágio é tipada e explícita. `SalesEvidence` nunca transforma “assinei” em verificação de assinatura, nem “resumo da venda” em serviço ativo.
- Não se alteraram controles de acesso, posse de fila, instâncias ou restrições de dados. A interpretação continua confinada à conversa já autorizada pelo sistema.
- A política histórica de campanhas, concessões e exceções **não foi alterada automaticamente**. Deve ser revisada pela gestão antes de qualquer mudança de preço, promoção ou promessa.

## Estados observáveis

`NOVO_CONTATO → INTERESSE_CONFIRMADO → DIAGNOSTICO → PROPOSTA_APRESENTADA → OBJECAO_PENDENTE → ACORDO_COMERCIAL → CRM_REGISTRADO → CONSULTA_CREDITO → CONTRATO_ENVIADO → ASSINATURA_CONFIRMADA → INSTALACAO_AGENDADA → CONTRATO_ATIVO`.

`PERDIDO` e `TRANSFERIDO_A_HUMANO` são estados de saída verificáveis, não progresso de conversão. Cada estado pós-acordo exige evento de sua integração; não inferir estados com base em texto do WhatsApp.

## Matriz de cenários para treinamento / QA

| Caso | Sinal | Comportamento esperado |
| --- | --- | --- |
| 01 Quero contratar hoje | PURCHASE_INTENT | Próxima etapa sem reapresentação genérica |
| 02 Até R$ 90 eu fecho | CONDITIONAL_BUDGET | Validar catálogo, não inventar desconto |
| 03 Esposo + segunda | DECISION_MAKER | Respeitar decisor e dia |
| 04 Não tenho interesse, parem | STOP_CONTACT | Opt-out, sem retorno |
| 05 Só fibra | ONLY_FIBER | Continuar sem empurrar móvel |
| 06 Cinco meses no imóvel | FIDELITY | Explicar multa e exceção apenas validada |
| 07 Instalar amanhã | URGENCY_INSTALL | Consultar cobertura/agenda |
| 08 Medo de golpe | SECURITY_CONCERN | Explicar canal e finalidade |
| 09 Já assinei | SIGNATURE_REPORTED | Verificar status real |
| 10 Link não abre | SIGNATURE_ERROR | Diagnosticar erro do link |
| 11 CRM falhou | Evidência sem ID | Não registrar sucesso fictício |
| 12 IA não autorizada na fila | Controle de fila existente | Proibir resposta espontânea |
| 13 Outro vendedor mesmo número | Isolamento existente | Não misturar históricos |
| 14 Áudio não transcrito | AUDIO_UNTRANSCRIBED | Não inventar conteúdo |
| 15 Crédito reprovado | CREDIT_REJECTED | Consultar resultado real, só contestação permitida |
| 16 Retorno às 15h | RETURN_COMMITMENT | Não disparar antes da hora |

Os testes do arquivo `tests/test_sulia_commercial_protocol_20261123.py` verificam classificação, evidências, orientações e horário em unidade. **Não são uma homologação ponta a ponta com CRM, Z-API e WhatsApp.**

## Pendente para etapas posteriores

1. Ligar protocolo, oportunidade, consulta, assinatura, OS e ativação com IDs reais e auditoria de evento de origem (não expor PII nos relatórios).
2. Reavaliar com gestão instruções comerciais legadas que admitem concessões e apresentam prazos predefinidos, antes de ampliar regras determinísticas.
3. Revisar política de atendimento humano e exceções com responsáveis e permissões, sem abrir acesso entre filas.
4. Rodar comparação offline em histórico anonimizado, revisão humana estratificada e análise antes/depois por origem, produto e cidade.
5. Medir tempo de resposta entregue, leads quentes sem retorno, opt-out, conversão por estágio verificado e falsas promessas.
