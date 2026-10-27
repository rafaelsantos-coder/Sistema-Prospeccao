# Roadmap de evolução — Sulnet Prospecta

## Versão 0.1 — MVP funcional entregue

- Autenticação e perfis internos.
- Estrutura por unidade, equipe e responsável.
- CRM, funil, atividades, follow-ups e metas.
- Score Sulnet e preservação do score legado.
- Tráfego pago separado.
- Busca Apify preparada.
- Campanhas com aprovação gerencial.
- Rate limiter legado compatível.
- Importação de `prospects.db`.
- Auditoria, exportação CSV, Docker e testes.

## Versão 0.2 — Homologação operacional

1. Cadastrar usuários e unidades definitivos.
2. Importar os leads existentes.
3. Configurar Apify e validar 3 buscas controladas.
4. Definir regras de distribuição por cidade/equipe.
5. Homologar score com Comercial e Marketing.
6. Configurar os modelos de mensagens Sulnet.
7. Treinar um grupo-piloto.
8. Acompanhar qualidade de cadastro e SLA por duas semanas.

**Critérios de aceite:** nenhum lead sem responsável; nenhuma oportunidade ativa sem próxima ação; tráfego pago preservado; duplicação abaixo de 2%; painel diário confiável.

## Versão 0.3 — Comunicação integrada

- Evolution API com webhook de envio, entrega, leitura e resposta.
- Resend com eventos de entrega, abertura, rejeição e opt-out.
- Caixa de entrada comercial unificada.
- Régua D+3, D+10 e D+20 executada por worker persistente.
- Fila Redis/RQ ou Celery, retentativas e idempotência.
- Aprovação de campanha por amostra e limite por unidade.
- Lista de bloqueio e preferência de contato conforme LGPD.

**Critérios de aceite:** nenhum envio duplicado; respostas interrompem automaticamente a régua; opt-out bloqueia todos os canais; logs completos por mensagem.

## Versão 0.4 — Integrações Sulnet

- Consulta de cliente ativo para evitar prospecção indevida.
- Integração com Voalle/ConectAI.
- Consulta de cobertura e viabilidade por endereço.
- Produtos: Fibra, Sulmóvel, Link Dedicado, VPN, Nuvem, Hosting e Voz.
- Propostas, análise de crédito, contrato, BKO, agendamento e instalação.
- Sincronização de venda ganha e MRR real.

**Critérios de aceite:** oportunidade acompanha o processo até a instalação; dados essenciais não precisam ser redigitados; venda e receita são reconciliadas com o sistema oficial.

## Versão 0.5 — Gestão e inteligência

- Redistribuição automática de leads parados, excluindo tráfego pago.
- SLA por origem, equipe, unidade e vendedor.
- Qualidade do lead, do contato e da venda.
- Conversão, ciclo, CAC, MRR, receita incremental e produtividade.
- Forecast por etapa e probabilidade.
- IA para resumo do histórico, sugestão de próxima ação, copy assistida e treinamento de objeções.
- Alertas de baixa atividade, follow-up vencido e carteira sem movimento.

## Governança

- Produção em PostgreSQL com backup diário.
- HTTPS, segredo de sessão, política de senha ou SSO.
- Logs de auditoria e retenção definida.
- Perfis de acesso revisados trimestralmente.
- Chaves somente em cofre/variáveis de ambiente.
- Processo de homologação antes de qualquer automação de envio.
