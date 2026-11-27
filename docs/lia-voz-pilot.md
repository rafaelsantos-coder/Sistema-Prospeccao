# Lia Voz - piloto isolado (fase 1)

SDRIA de ligacoes da Sulnet. O modulo novo nao altera WhatsApp, campanhas,
banco de dados, SIP, rotas HTTP, credenciais ou deploys existentes.

## Implementado nesta fase

- Sessao Realtime GA, modelo configuravel (padrao gpt-realtime-2.1-mini),
  voz marin, audio PCM16 mono 24 kHz e VAD no servidor.
- Transporte WebSocket assincrono, chave recebida do chamador, opt-in
  explicito, limite de duracao e sem reconexao automatica.
- Fila de audio limitada a 15 segundos, interrupcao de reproducao,
  descarte de deltas atrasados e truncamento pela quantidade de audio
  entregue ao consumidor. A medida nao comprova audicao no aparelho.
- Prompt exclusivo de homologacao, sem dados de clientes ou ferramentas.
- Testes offline com socket simulado, sem custo de API ou telefonia.

## Teste local offline

    python -m pytest -q tests/test_lia_voz_realtime.py

A dependencia opcional e instalada apenas no ambiente de homologacao.
Acesso real a OpenAI, permissao do modelo e qualidade de voz precisam
ser verificados com uma chave de teste aprovada.

## Proxima fase (nao incluida neste PR)

1. Adaptar AudioMediaPort do PJSUA2, com resampling validado se necessario,
   transferencia segura entre threads e medicao de latencia.
2. Criar tela e teste individual administrativo, com numero de teste
   autorizado, protecao CSRF/RBAC e bloqueios existentes de telefonia.
3. Persistir memoria, treinamento e transcricoes em estruturas exclusivas
   de voz, com retencao, segregacao de acesso e aprovacao de aprendizado.
4. Conectar ferramentas CRM validadas, opt-out imediato, agendamento
   idempotente e transferencia assistida. Nao inventar viabilidade,
   preco, cobertura ou aprovacao de credito.
5. Homologar chamadas Native em ambiente controlado. Distinguir aceite
   de SIP REFER de atendimento humano confirmado. Sem ativar campanhas
   automaticamente; exigir validacao de operacao e conformidade.

Referencias:
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/reference/resources/realtime/client-events
- https://developers.openai.com/api/reference/resources/realtime/server-events

## Fase 2: adaptador nativo isolado

Adicionado MediaBridge e porta AudioMediaPort (24 kHz, mono, 16 bits, 20 ms),
com fila de entrada limitada a 500 ms e silencio na saida apos fechamento.
PJSIP faz a conversao do formato da conferencia. Os callbacks nao usam rede.
O protocolo Realtime sincroniza leitura e interrupcao com a thread de midia.
O runner encerra as tarefas WebSocket em caso de falha ou fechamento.
O adaptador ainda nao e chamado por nenhum worker de producao.
Testes offline com SWIG simulado: python -m pytest -q tests/test_lia_voz_media.py.
Proximo: testar com wheel PJSUA2 2.15.1.post1 e RTP real de bancada,
validar cancelamento, desconexao e autorizacao antes de ligar pela Native.
