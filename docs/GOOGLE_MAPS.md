# Google Maps opcional para mapa de credito

A mudanca altera apenas a base cartografica da pagina /reports/credit-map. Os indicadores, classificacoes, marcadores de enderecos aprovados, importacao de CSV e exportacao Google My Maps continuam iguais.

## Railway: configurar em staging primeiro

Variaveis do servico web:
- CREDIT_MAP_PROVIDER=google
- GOOGLE_MAPS_BROWSER_KEY=(chave de producao do Google Cloud)
- GOOGLE_MAPS_MAP_ID=DEMO_MAP_ID (substituir pelo Map ID do seu projeto quando houver)

Sem chave ou com provedor diferente de google, OpenStreetMap permanece padrao. Nenhuma chave deve ser adicionada ao GitHub.

## Google Cloud Console

1. Criar chave de producao. Chaves Maps Hosted Demo nao sao para producao.
2. Ativar Maps JavaScript API e associar faturamento.
3. Restringir a chave do navegador por websites: https://web-staging-d514.up.railway.app/* e, separadamente, https://sulnet-prospecta.up.railway.app/*.
4. Restringir a chave a Maps JavaScript API. Nao reutilizar chave de browser para API server-side.
5. Definir alertas e cotas de faturamento.

A chave fica exposta no navegador por design. A protecao vem das restricoes por referer e API.

## Funcionamento

- Google Maps exibe bairros em circulos, ruas com pontos, enderecos aprovados em triangulos, com os mesmos popups e filtros.
- Nao usa o HeatmapLayer antigo, descontinuado.
- Se falhar o carregamento do Google, o mapa tenta OpenStreetMap.
- A geocodificacao do servidor continua sendo Nominatim com cache, portanto nao ganha localizacao de enderecos mais rapida pela mudanca de base cartografica.
- Dados individuais como CPF, nome e telefone nao sao enviados ao Google Maps.
- Validar em staging antes de ativar na producao.
