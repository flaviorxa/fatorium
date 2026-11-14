# FATORIUM

Inteligência quantitativa para investimentos: plataforma local de análise fundamentalista para ações,
FIIs, ETFs, BDRs e cripto da B3. Coleta dados de fontes públicas, calcula indicadores, monta
rankings e backtests, e serve um site próprio com login — tudo em um container, sem chave de API e
sem serviço pago.

[![CI](https://github.com/flaviorxa/fatorium/actions/workflows/ci.yml/badge.svg)](https://github.com/flaviorxa/fatorium/actions/workflows/ci.yml)
[![Licença: AGPL v3](https://img.shields.io/badge/licen%C3%A7a-AGPL--3.0-blue.svg)](LICENSE)

> Conteúdo informativo. Não é recomendação de investimento nem consultoria regulada pela CVM/Anbima.

## O que tem dentro

- **Coleta** de cotações, proventos e fundamentos: Yahoo Finance (via `yfinance`), Fundamentus,
  CVM (informes diários), Status Invest, Funds Explorer, MSN e OKX para o par em reais.
- **Indicadores**: P/L, P/VP, DY, EV/EBITDA, margens, dívida, Bazin, Graham, retorno total 12m,
  regularidade de proventos 24m, técnicos (médias, RSI, bandas) e Trend Template.
- **Rankings** por estratégia (dividendos, valor, qualidade, FIIs, LTM), screening com filtros,
  comparação lado a lado, backtest sem look-ahead e alertas de mudança de ranking.
- **Site** em FastAPI + Jinja com tema claro/escuro, gráfico de preços e exportação CSV.
- **Agendador** interno: coleta e rankings rodam sozinhos, com frequência e horário configuráveis
  na tela **Fontes**.
- **SQLite** (WAL) como banco — um arquivo, fácil de copiar e fazer backup.

## Instalação rápida (Docker)

Com login (senha aleatória de 32 caracteres no `.env`):

```bash
mkdir -p ~/fatorium && cd ~/fatorium
curl -fsSL https://raw.githubusercontent.com/flaviorxa/fatorium/main/docker-compose.yml -o docker-compose.yml
printf 'INVEST_SENHA=%s\n' "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)" > .env
chmod 600 .env
docker compose up -d
```

A senha é opcional: sem `INVEST_SENHA` no `.env` o site sobe aberto, sem login. Para isso, pule o
`printf`:

```bash
mkdir -p ~/fatorium && cd ~/fatorium
curl -fsSL https://raw.githubusercontent.com/flaviorxa/fatorium/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Abra `http://localhost:8080` — usuário padrão `invest`, quando a autenticação está ligada.
A primeira execução dispara a coleta inicial em segundo plano (~15–20 min para o universo padrão);
até ela terminar, as telas mostram pouco ou nenhum dado.

Ou, em uma linha (cria o diretório e sobe; use `--senha` para gerar a senha):

```bash
curl -fsSL https://raw.githubusercontent.com/flaviorxa/fatorium/main/install.sh | sh -s -- --senha
```

### CasaOS

O `docker-compose.yml` traz metadados `x-casaos`, então dá para instalar como app personalizado
(CasaOS → App Store → "Install a customized app" → colar o compose) ou usar o `install.sh` acima.
O volume `fatorium-data` guarda banco, cache da CVM e a lista de ativos.

> A imagem é construída **só no GitHub Actions** e publicada em `ghcr.io/flaviorxa/fatorium`.

## Rodando sem Docker

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
INVEST_SENHA=uma-senha-bem-longa-de-teste .venv/bin/python -m uvicorn app.api.main:app --port 8000
```

Testes: `.venv/bin/python -m pytest -q` (314 testes, nenhum acessa a rede).

## Configuração

| Variável | Padrão | Para que serve |
|---|---|---|
| `INVEST_SENHA` | — (opcional) | Com 16+ caracteres exige HTTP Basic; vazio roda sem login. |
| `INVEST_USUARIO` | `invest` | Usuário do Basic Auth. |
| `FATORIUM_PORTA` | `8080` | Porta publicada pelo compose. |
| `INVEST_LISTA_ATIVOS` | `config/universo.md` (no container, `/app/data/universo.md`) | Lista Markdown do universo. |
| `INVEST_COLETAR_NO_BOOT` | `1` | Na primeira execução, roda a coleta inicial em segundo plano. `0` desliga. |

A lista de ativos também é editável pela tela **Cadastro de Ativos**. O arquivo é a fonte de
verdade do universo: cada seção (`Ações`, `BDRs`, `ETFs`, `Fundos Imobiliários e Fi-Infras`,
`Criptomoedas`) tem os tickers separados por vírgula.

## Fontes, limites e responsabilidade

Os dados vêm de sites públicos, sem contrato de API: Yahoo Finance, Fundamentus, CVM, Status
Invest, Funds Explorer, MSN, Research BTG, Genial Analisa e OKX. **É melhor esforço.** Fonte que
muda de layout, limita requisições ou bloqueia a região onde você roda deixa de responder — a tela
**Fontes** mostra qual falhou e quando. Alguns dados (preço-alvo de BTG e Genial) cobrem só parte
do universo.

O uso de cada fonte está sujeito aos termos do próprio site. Este projeto não redistribui base de
dados de terceiros: ele consulta e guarda o resultado na sua máquina.

## Segurança e escopo

- Por padrão o site sobe **sem autenticação** — pensado para rede doméstica, VPN ou atrás de um
  reverse proxy. Defina `INVEST_SENHA` (16+ caracteres) para exigir login, e nunca publique a porta
  na internet sem TLS e senha.
- O container fala HTTP. Para expor na internet, coloque Caddy/Traefik/nginx na frente com TLS.
- **Um worker apenas**: o agendador vive dentro do processo; duas réplicas duplicariam os jobs.
- Sem integração com corretora, sem execução de ordens, sem multi-usuário.

## Estrutura

```
app/api/        rotas HTTP e montagem do FastAPI
app/motor/      indicadores, rankings, screening, backtest, consolidação
app/fontes/     um adaptador por fonte (contrato em fontes/base.py)
app/web/        consultas ao banco, formatação, autenticação, links
app/templates/  telas Jinja
app/static/     CSS e JS (externos: o CSP não permite inline)
scripts/        coletar.py e utilitários de manutenção
tests/          suíte pytest, sem rede
```

## Contribuindo

Issues e pull requests são bem-vindos — fork, branch, `pytest -q` verde e abra o PR descrevendo o
que mudou e como verificou. Preferências do projeto: sem dependência nova para o que a biblioteca
padrão resolve, CSS/JS sempre em arquivo externo (CSP `script-src 'self'`), e toda fonte de dado
nova entra como adaptador em `app/fontes/` com teste que não toca a rede.

## Apoie o projeto

O FATORIUM é gratuito, aberto e roda na sua máquina — sem assinatura, sem chave de API e sem
telemetria. Se ele te economiza tempo na análise, dá para retribuir com uma doação ou um café:
isso paga horas de manutenção, novas fontes de dados e as correções que sempre aparecem quando um
site muda de layout.

**Pix (chave aleatória)**

```
e6a254da-9d7f-4a69-80ce-6bbb2b242b01
```

**Bitcoin (BTC)**

```
bc1q6n8ky26je0ngx5m5w347g3p52jmu6kl04sprlx
```

Qualquer valor é bem-vindo. Doação é espontânea: não compra suporte, prioridade em issues nem
influência nas decisões do projeto.

## Licença

**GNU AGPL v3.0** — use, modifique e redistribua livremente, mantendo o aviso de copyright e o
código-fonte aberto. Se você rodar uma versão modificada como serviço de rede, o artigo 13 obriga a
oferecer o código-fonte dessa versão a quem interage com ela. É a mesma licença da imagem publicada
no GHCR. Veja [LICENSE](LICENSE).
