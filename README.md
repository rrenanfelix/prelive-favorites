# WAVE Pre-Live Favorites

Dashboard para escanear jogos pre-live ou historico do dia usando API-Football.

Filtros aplicados:

- mandante favorito com odd entre `1.50` e `2.30`
- mandante venceu pelo menos `3` dos ultimos `5` H2H
- mandante venceu pelo menos `5` dos ultimos `10` jogos em casa
- mandante esta pelo menos `7` posicoes acima do visitante na tabela

## Rodar local

```bash
cd /Users/renan/.openclaw/workspace/dashboards/prelive-favorites
python3 app.py
```

Depois abra:

```text
http://localhost:8891
```

Por padrao a tela ja seleciona amanha, para facilitar o scan de meia-noite.
Use `Historico do dia` para analisar tambem jogos que ja terminaram e marcar as entradas como `GREEN`, `RED` ou `Pendente`.
Toda entrada validada fica salva em `data/validated_history.json`. A aba `Historico completo` mostra o acumulado de todos os jogos que ja passaram nos filtros, com total de `GREEN`, `RED` e pendentes.
Use `Atualizar placares` para conferir novamente os jogos pendentes sem rodar um novo scanner.

## Variaveis

- `API_FOOTBALL_KEY`: chave API-Football. Obrigatoria no online; configure como segredo/variavel de ambiente.
- `PORT`: porta do servidor, default `8891`.
- `DATA_DIR`: pasta para cache do ultimo scan.
- `PRELIVE_ODD_MIN`, `PRELIVE_ODD_MAX`
- `PRELIVE_H2H_MIN_HOME_WINS`
- `PRELIVE_HOME_FORM_MIN_WINS`
- `PRELIVE_MIN_RANK_GAP`
- `PRELIVE_AUTO_SCAN`: `1` para atualizar automaticamente sem clicar.
- `PRELIVE_AUTO_SCAN_DAYS_AHEAD`: quantos dias a frente escanear; default recomendado `1` para amanha.
- `PRELIVE_AUTO_SCAN_INTERVAL_MINUTES`: intervalo entre scans automaticos; minimo `5`, recomendado `30`.
- `PRELIVE_AUTO_SCAN_INCLUDE_PLAYED`: `1` para incluir jogos ja iniciados/finalizados no auto-scan.

## Deploy online

GitHub Pages nao serve este app porque ele precisa rodar Python e chamar a API-Football.
Use GitHub apenas para hospedar o codigo, e conecte o repositorio em um host Python como Render ou Railway.

### Render

O arquivo `render.yaml` ja define:

- web service Python
- comando `python3 -u app.py`
- healthcheck em `/health`
- auto-scan ligado para amanha a cada 30 minutos
- `API_FOOTBALL_KEY` como segredo obrigatorio

Passos:

1. Crie um repositorio contendo os arquivos desta pasta.
2. No Render, crie um Blueprint a partir do repositorio.
3. Configure a variavel secreta `API_FOOTBALL_KEY`.
4. Abra a URL publica do serviço.

### Railway

Tambem funciona como web service Python:

- Start command: `python3 -u app.py`
- Variables:
  - `API_FOOTBALL_KEY`
  - `PRELIVE_AUTO_SCAN=1`
  - `PRELIVE_AUTO_SCAN_DAYS_AHEAD=1`
  - `PRELIVE_AUTO_SCAN_INTERVAL_MINUTES=30`
