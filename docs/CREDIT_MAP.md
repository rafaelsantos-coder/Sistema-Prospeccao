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
- Importação mantém somente totais por cidade/bairro/mês/decisão/assinatura. Os dados pessoais da consulta são descartados após a agregação em memória.
- Perfis ADMIN e GESTOR podem substituir a base; SUPERVISOR somente consulta.
- A API somente retorna bairros com **10 ou mais consultas** no período e não expõe nome, CPF, telefone, restrições, score, coordenadas de residências ou datas específicas.
- O mapa usa centro aproximado dos bairros obtido por geocodificação do OpenStreetMap (Nominatim); bairros sem localização confiável ficam no ranking mas não aparecem na camada geográfica.
- Não usar geografia como fator para **aprovar ou rejeitar** crédito, e nem para excluir bairros de oportunidades de atendimento. O objetivo do painel é diagnosticar funil, cobertura e volume comercial agregados.
- Retenção, acesso e base legal para consultas de crédito devem ser aprovados pelo encarregado de dados da Sulnet.

## Limitações

- O mapa usa círculos de intensidade sobre o OpenStreetMap. O uso dos mapas e do Nominatim públicos deve ser leve e interno; exige a atribuição "© OpenStreetMap" (já exibida no mapa).
- Filtro por mês, não por dia. A mesma consulta pode repetir um CPF com outro protocolo; totais são consultas, não pessoas únicas.
- A geocodificação de bairro não informa posição do cliente e pode falhar. Resultados incompletos não devem ser interpretados como ausência de consultas.
- Não houve publicação automática no sistema de produção nesta entrega.
