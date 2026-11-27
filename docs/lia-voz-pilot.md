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


## Fase 3: tela e orquestracao de teste individual

Tela administrativa /voice/lia-voz, acessivel pelo dashboard de voz.
POST com CSRF, ADMIN, confirmacao explicita, destino unico permitido,
blacklist, conta Native 1804, janela de solicitacao de 90 s e limites de
3 testes/hora e intervalo de 3 minutos. A fila usa estados LIA_ distintos
em voice_test_calls, sem nova migracao, sem campanhas ou chamadas automaticas.

Apenas o motor isolado processa o teste quando todas as flags abaixo estiverem
configuradas. O handshake Realtime ocorre antes do SIP INVITE. A ponte de
audio conecta somente apos SIP confirmado e RTP ativo; limite 90 s de conversa.
O encerramento cancela tarefas de rede, desconecta a porta nativa e encerra SIP.
Sem CRM, transferencia, transcricao persistida, memoria permanente ou ferramenta
de opt-out nesta fase: somente teste interno autorizado, nao clientes.

**Nao ativar em producao.** A mesma conta SIP pode estar em uso por outros
workers, projetos Railway ou aparelhos. O flag de exclusividade e uma
confirmacao operacional, nao uma trava distribuida entre PABXs/projetos.
Usar ambiente de homologacao sem campanhas e sem outro dispositivo/worker
registrado nessa conta; validar a janela com a operacao/telefonia.
Apenas depois dos testes nativos, configurar explicitamente no web e no
motor isolado:
- LIA_VOZ_TEST_ENABLED=true
- LIA_VOZ_TEST_EXCLUSIVE_SIP=true
- LIA_VOZ_TEST_PHONE (numero interno autorizado, nao colocar no GitHub)
- LIA_VOZ_TEST_NATIVE_PREFIX (uma das cinco linhas)
No motor, usar docker/lia-voz-pilot.Dockerfile, uma replica, habilitar
VOICE_ENGINE_WORKER_ENABLED, manter VOICE_CAMPAIGN_OUTBOUND_ENABLED e
VOICE_TRANSFER_TESTS_ENABLED desabilitados, configurar LIA_VOZ_OPENAI_API_KEY
via segredo separado e liberar a trava geral apenas no ambiente de teste.

Testes adicionados para handshake, revogacao de autorizacao, registro SIP e
encerramento remoto simulado. O CI de imagem nativa tambem executa o benchmark
PCM entre duas portas PJSUA2 sem SIP, rede externa ou OpenAI.

Nenhum segredo ou flag de ativacao foi configurado neste PR. Os contadores de
quadros e SIP 200 nao comprovam que a pessoa ouviu e respondeu; validar no
aparelho. Se houver falha de rede ou midia, nao repetir automaticamente.
