# Sulnet Prospecta — Mapa de Calor de Crédito

Painel integrado ao sistema em `/reports/credit-map` com mapa OpenStreetMap (Leaflet), sem chave nem cadastro.

## Instalação (repositório Sistema-Prospeccao)

1. Copiar arquivos novos do módulo para `app/` e `alembic/versions/`.
2. Aplicar as alterações indicadas no arquivo `INTEGRACAO.md`.
3. Rodar `python -m app.migrate` (ou manter pre-deploy do Railway, que já executa migrações).
4. Não há chave de mapa. O servidor localiza os bairros pelo Nominatim (OpenStreetMap), no máximo 1 consulta por segundo, e guarda o resultado na tabela `credit_map_geocodes`; a primeira abertura de uma cidade nova leva alguns segundos por lote de bairros.
5. Visitar `/reports/credit-map` autenticado e importar `analises_credito_completo.csv` com perfil ADMIN/GESTOR.

## Segurança/LGPD

- Nunca colocar o CSV original no GitHub ou em pasta pública. Ele contém CPF, telefone, data de nascimento e dados de crédito.
- Importação mantém os totais por cidade/bairro/rua/mês/decisão/assinatura e, **somente dos clientes aprovados**, o endereço (rua e número, bairro, mês e se assinou contrato), usado nos triângulos do mapa. CPF, nome, telefone, data de nascimento, score e restrições são descartados após o processamento em memória. A guarda do endereço dos aprovados foi autorizada pela Sulnet em 16/11/2026.
- Perfis ADMIN e GESTOR podem substituir a base; SUPERVISOR somente consulta.
- A API somente retorna bairros com **10 ou mais consultas** no período e não expõe nome, CPF, telefone, restrições, score ou datas específicas. A rota `/api/reports/credit-map/addresses` devolve rua, número e posição dos clientes aprovados (com e sem contrato) para os perfis ADMIN, GESTOR e SUPERVISOR; reprovados nunca aparecem por endereço.
- Para localizar um endereço, o servidor envia rua, número e cidade ao Nominatim (OpenStreetMap), sem qualquer identificação do cliente, e guarda a posição em `credit_map_geocodes`.
- Os círculos de bairro usam o centro aproximado obtido por geocodificação do OpenStreetMap (Nominatim); bairros sem localização confiável ficam no ranking mas não aparecem na camada geográfica.
- Não usar geografia como fator para **aprovar ou rejeitar** crédito, e nem para excluir bairros de oportunidades de atendimento. O objetivo do painel é diagnosticar funil, cobertura e volume comercial agregados.
- Retenção, acesso e base legal para consultas de crédito devem ser aprovados pelo encarregado de dados da Sulnet.

## Leitura do mapa

- **Bairro e rua**: verde quando aprovados ≥ reprovados (empate fica verde), vermelho quando há mais reprovados. Cor única, sem variação de tom; o tamanho do círculo segue a opção escolhida em "Visualizar".
- **Triângulo verde**: endereço de cliente aprovado com contrato assinado.
- **Triângulo amarelo**: endereço de cliente aprovado ainda sem contrato.
- Quando o OpenStreetMap conhece a rua mas não o número, o triângulo fica no meio da rua e o balão avisa que a posição é aproximada. Clientes no mesmo ponto são afastados alguns metros para não ficarem um sobre o outro.
- Os triângulos só aparecem depois de importar um CSV com as colunas Rua e Número; a primeira abertura localiza cerca de um endereço por segundo e guarda o resultado.


## Coordenadas do CRM — regra de triângulos (coluna Q)

- Ao importar um CSV com a coluna opcional `Coordenadas`, **somente clientes aprovados com coordenadas válidas** aparecem como triângulos. Reprovados e aprovados sem GPS continuam nos indicadores agregados de bairro, rua, mês e cidade, mas não viram pinos.
- O importador aceita latitude/longitude decimais e coordenadas em graus, minutos e segundos, rejeitando valores ausentes, textos, zeros e posições fora dos limites amplos do Rio Grande do Sul.
- Para os triângulos com GPS, não há pesquisa de endereço no Nominatim: o sistema usa a latitude/longitude já informadas no CRM. Não desloca o ponto artificialmente para evitar sobreposição.
- Verde: aprovado com contrato assinado; amarelo: aprovado sem contrato. Endereços são apresentados somente quando disponíveis.
- **A coordenada pode refletir a posição do lançamento/contratação, e não a porta do imóvel.** O sistema não pode presumir precisão predial apenas pelo preenchimento do campo.
- CSVs antigos sem coluna `Coordenadas` continuam compatíveis com a geocodificação anterior. Para ativar a nova regra nos dados já existentes, o CSV atualizado precisa ser reimportado no painel; a mudança do código não atualiza automaticamente os registros do banco.
- CSVs brutos com CPF, nome ou telefone nunca devem ser adicionados ao repositório; o processador descarta esses dados e armazena somente agregados e localização aprovada.

## Exportar para o Google My Maps

O botão **Arquivo para Google My Maps** (ADMIN e GESTOR) baixa um CSV com o endereço completo (rua, número, bairro, cidade) dos clientes aprovados no período e município escolhidos, com a coluna Situação (Contrato assinado / Aprovado sem contrato). O Google My Maps localiza cada endereço, inclusive o número da casa, sem chave de API nem custo. O arquivo não leva nome, CPF ou telefone, e cada download é registrado na auditoria (`CREDIT_MAP_EXPORTED_MY_MAPS`).

- O My Maps não tem interface para automação: a importação do arquivo é feita na tela do Google. Mantenha o mapa **privado**, compartilhado só com as contas da equipe, porque ele lista endereços de clientes.
- No My Maps: Criar novo mapa > Importar > escolher o arquivo > posicionar pela coluna Endereço > título pela coluna Bairro > Estilo > "Agrupar por" Situação, com verde para Contrato assinado e amarelo para Aprovado sem contrato.

## Limitações

- O mapa usa círculos de intensidade sobre o OpenStreetMap. O uso dos mapas e do Nominatim públicos deve ser leve e interno; exige a atribuição "© OpenStreetMap" (já exibida no mapa).
- Filtro por mês, não por dia. A mesma consulta pode repetir um CPF com outro protocolo; totais são consultas, não pessoas únicas.
- A geocodificação pode falhar ou cair só na rua. Resultados incompletos não devem ser interpretados como ausência de consultas.
- Não houve publicação automática no sistema de produção nesta entrega.
