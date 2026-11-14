"""API HTTP da plataforma de investimentos."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode

import pandas as pd
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import agenda, alertas, config, db, universo
from app.fontes import cvm, msn
from app.motor import (
    backtest,
    consolidado,
    estrategias,
    rankings,
    screening,
    tecnicos,
    valuation,
)
from app.motor.indicadores import avaliar_fundamentalista
from app.web import consultas, formato, links
from app.web.auth import (
    AutenticacaoMiddleware,
    NaoIndexarMiddleware,
    validar_ambiente,
)

logger = logging.getLogger(__name__)


async def lifespan(_: FastAPI) -> Any:
    validar_ambiente()
    encerradas = db.encerrar_coletas_orfas()
    logger.info("coletas órfãs encerradas na partida: %d", encerradas)
    agenda.iniciar_scheduler()
    try:
        yield
    finally:
        agenda.parar_scheduler()


app = FastAPI(title="FATORIUM", lifespan=lifespan)
app.add_middleware(AutenticacaoMiddleware)
app.add_middleware(NaoIndexarMiddleware)

DIRETORIO_TEMPLATES = config.RAIZ / "app" / "templates"
DIRETORIO_ESTATICOS = config.RAIZ / "app" / "static"
app.mount(
    "/static", StaticFiles(directory=str(DIRETORIO_ESTATICOS)), name="static"
)
templates = Jinja2Templates(directory=str(DIRETORIO_TEMPLATES))
templates.env.globals["formato"] = formato
templates.env.globals["nome_estrategia"] = consultas.nome_estrategia
templates.env.globals["nome_classe"] = consultas.nome_classe


def _versao_estatica() -> str:
    """mtime dos assets versionados: muda a URL quando o arquivo muda (cache)."""

    try:
        return str(
            int(
                max(
                    (DIRETORIO_ESTATICOS / nome).stat().st_mtime
                    for nome in ("estilo.css", "tema.js", "confirmar.js", "menu.js")
                )
            )
        )
    except OSError:
        return "0"


templates.env.globals["versao_estatica"] = _versao_estatica


@app.middleware("http")
async def cabecalhos_seguranca(request: Request, call_next: Any) -> Any:
    resposta = await call_next(request)
    resposta.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "same-origin"
    return resposta


def _pede_html(request: Request) -> bool:
    caminho = request.url.path
    if caminho.startswith("/api/") or caminho in {
        "/saude",
        "/docs",
        "/redoc",
        "/openapi.json",
    }:
        return False
    aceito = request.headers.get("accept", "")
    return "text/html" in aceito or aceito in {"", "*/*"}


def _erro_html(request: Request, codigo: int, mensagem: str) -> HTMLResponse:
    try:
        resposta = templates.TemplateResponse(
            request=request,
            name="erro.html",
            context={"codigo": codigo, "mensagem": mensagem},
            status_code=codigo,
        )
        return resposta
    except Exception:  # pragma: no cover - último recurso se o template falhar
        logger.exception("falha ao renderizar página de erro")
        return HTMLResponse(
            status_code=codigo,
            content=(
                '<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
                f"<title>Erro {codigo}</title><body><h1>Erro {codigo}</h1>"
                f"<p>{mensagem}</p><p>Conteúdo informativo — não é recomendação "
                "CVM/Anbima.</p></body></html>"
            ),
        )


@app.exception_handler(HTTPException)
async def tratar_http_exception(request: Request, exc: HTTPException) -> Any:
    detalhe = exc.detail
    if _pede_html(request):
        return _erro_html(request, exc.status_code, str(detalhe))
    if isinstance(detalhe, dict) and "erro" in detalhe:
        corpo = detalhe
    else:
        corpo = {"erro": str(detalhe)}
    return JSONResponse(status_code=exc.status_code, content=corpo)


@app.exception_handler(RequestValidationError)
async def tratar_validacao(request: Request, exc: RequestValidationError) -> Any:
    if _pede_html(request):
        return _erro_html(request, 422, "parâmetros inválidos")
    return JSONResponse(status_code=422, content={"erro": "parâmetros inválidos"})


@app.exception_handler(Exception)
async def tratar_erro(request: Request, exc: Exception) -> Any:
    logger.exception("erro não tratado em %s", request.url.path)
    if _pede_html(request):
        return _erro_html(request, 500, "erro interno")
    return JSONResponse(status_code=500, content={"erro": "erro interno"})


def _linha_para_dict(linha: Any) -> dict[str, Any]:
    return {chave: linha[chave] for chave in linha.keys()}


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        "User-agent: *\nDisallow: /\n", media_type="text/plain"
    )


@app.get("/saude")
def saude() -> dict[str, Any]:
    with db.conectar() as conn:
        ativos = conn.execute("SELECT COUNT(*) FROM ativos").fetchone()[0]
        cotacoes = conn.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0]
    return {
        "ok": True,
        "banco": str(config.DB_PATH),
        "ativos": int(ativos),
        "cotacoes": int(cotacoes),
    }


@app.get("/api/ativos")
def listar_ativos(
    classe: str | None = Query(default=None),
    busca: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    sql = """
        SELECT a.ticker, a.nome, a.classe, a.setor, a.moeda, a.fonte,
               a.atualizado_em,
               c.fechamento AS ultimo_fechamento,
               c.data AS ultima_data
          FROM ativos a
          LEFT JOIN cotacoes c
            ON c.ticker = a.ticker
           AND c.data = (SELECT MAX(data) FROM cotacoes x WHERE x.ticker = a.ticker)
         WHERE 1 = 1
    """
    parametros: list[Any] = []
    if classe:
        sql += " AND a.classe = ?"
        parametros.append(classe)
    if busca:
        sql += " AND (LOWER(a.ticker) LIKE ? OR LOWER(COALESCE(a.nome, '')) LIKE ?)"
        termo = f"%{busca.lower()}%"
        parametros.extend([termo, termo])
    sql += " ORDER BY a.ticker"

    with db.conectar() as conn:
        linhas = conn.execute(sql, parametros).fetchall()
    return [_linha_para_dict(linha) for linha in linhas]


@app.get("/api/ativos/{ticker}")
def detalhar_ativo(ticker: str) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    with db.conectar() as conn:
        ativo = conn.execute("SELECT * FROM ativos WHERE ticker = ?", (ticker,)).fetchone()
        if ativo is None:
            raise HTTPException(status_code=404, detail=f"ativo {ticker} não encontrado")

        cotacoes = conn.execute(
            """
            SELECT data, abertura, maxima, minima, fechamento,
                   fechamento_ajustado, volume, fonte
              FROM cotacoes
             WHERE ticker = ?
             ORDER BY data DESC
             LIMIT 250
            """,
            (ticker,),
        ).fetchall()
        cotacoes = list(reversed(cotacoes))

    detalhes = db.fundamentos_mais_recentes(ticker)
    fundamentos = {campo: info["valor"] for campo, info in detalhes.items()}
    datas_fundamentos = [
        str(info["data_ref"])
        for info in detalhes.values()
        if info.get("data_ref")
    ]
    referencia = max(datas_fundamentos) if datas_fundamentos else None
    fontes_fundamentos = sorted(
        {str(info["fonte"]) for info in detalhes.values() if info.get("fonte")}
    )

    return {
        "ativo": _linha_para_dict(ativo),
        "cotacoes": [_linha_para_dict(linha) for linha in cotacoes],
        "fundamentos": {
            "data_ref": referencia,
            "fontes": fontes_fundamentos,
            "campos": fundamentos,
            "detalhes": detalhes,
        },
        "indicadores": avaliar_fundamentalista(
            fundamentos, ticker=ticker, classe=ativo["classe"]
        ),
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    with db.conectar() as conn:
        coleta = conn.execute(
            """
            SELECT id, iniciado_em, terminado_em, tipo, status, resumo_json
              FROM coletas
             ORDER BY id DESC
             LIMIT 1
            """
        ).fetchone()
        falhas = conn.execute(
            "SELECT fonte, COUNT(*) AS quantidade FROM falhas GROUP BY fonte ORDER BY fonte"
        ).fetchall()

    ultima = None
    em_andamento = False
    if coleta is not None:
        ultima = _linha_para_dict(coleta)
        em_andamento = ultima.get("status") == "em_andamento"
        if em_andamento:
            ultima.pop("resumo_json", None)
            ultima["descricao"] = (
                "em andamento desde "
                + (formato.hora_br(ultima.get("iniciado_em")) or "início desconhecido")
            )
        else:
            try:
                ultima["resumo"] = json.loads(ultima.pop("resumo_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                ultima["resumo"] = {}

    return {
        "ultima_coleta": ultima,
        "coleta_em_andamento": em_andamento,
        "falhas_por_fonte": {linha["fonte"]: int(linha["quantidade"]) for linha in falhas},
    }


@app.get("/api/agenda")
def obter_agenda() -> list[dict[str, Any]]:
    return agenda.status()


@app.get("/api/alertas")
def listar_alertas(
    lidos: int | None = Query(default=None, ge=0, le=1),
) -> list[dict[str, Any]]:
    return alertas.listar(lidos=lidos)


@app.get("/api/fontes")
def fontes() -> dict[str, Any]:
    """Status resumido das fontes e pendências de mapeamento (lote 2f)."""

    with db.conectar() as conn:
        ultimas = conn.execute(
            """
            SELECT fonte, MAX(quando) AS ultima_falha, COUNT(*) AS falhas
              FROM falhas
             GROUP BY fonte
             ORDER BY fonte
            """
        ).fetchall()

    return {
        "taxa_administracao_cvm": {
            "campo": cvm.CAMPO_TAXA_ADMINISTRACAO,
            "texto": cvm.TEXTO_TAXA_ADMINISTRACAO,
        },
        "mapeamento_fii": consultas.mapeamento_fii(),
        "fontes": [_linha_para_dict(linha) for linha in ultimas],
    }


def _cotacoes_df(ticker: str) -> pd.DataFrame:
    with db.conectar() as conn:
        linhas = conn.execute(
            """
            SELECT data, abertura, maxima, minima, fechamento, volume
              FROM cotacoes
             WHERE ticker = ?
             ORDER BY data
            """,
            (ticker,),
        ).fetchall()
    return pd.DataFrame([_linha_para_dict(linha) for linha in linhas])


def _ultimo_nao_nulo(df: pd.DataFrame) -> dict[str, float | None]:
    ultimos: dict[str, float | None] = {}
    for coluna in df.columns:
        serie = df[coluna].dropna()
        ultimos[coluna] = float(serie.iloc[-1]) if len(serie) else None
    return ultimos


@app.get("/api/rankings")
def listar_rankings() -> list[dict[str, Any]]:
    ativas = rankings.estrategias_ativas()
    marcadores = ", ".join("?" for _ in ativas)
    with db.conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT r.id, r.estrategia, r.gerado_em, r.universo_n,
                   (SELECT COUNT(*) FROM ranking_itens i WHERE i.ranking_id = r.id) AS itens
              FROM rankings r
             WHERE r.estrategia IN ({marcadores})
               AND r.id IN (SELECT MAX(id) FROM rankings GROUP BY estrategia)
            """,
            ativas,
        ).fetchall()
    por_nome = {linha["estrategia"]: _linha_para_dict(linha) for linha in linhas}
    return [por_nome[estrategia] for estrategia in ativas if estrategia in por_nome]


@app.get("/api/ranking-consolidado")
def api_ranking_consolidado(
    min_listas: int | None = Query(default=None, ge=1),
    classe: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    return _consolidado(
        min_listas if min_listas is not None else int(consolidado.CONFIG_PADRAO["min_listas"]),
        classe or None,
    )


def _numero_screening(
    parametros: Any,
    chave: str,
    padrao: float | int,
    *,
    inteiro: bool = False,
    minimo: float | None = None,
) -> float | int:
    bruto = parametros.get(chave)
    if bruto in (None, ""):
        return padrao
    numero = formato.converter_numero(bruto)
    if numero is None:
        return padrao
    if minimo is not None and numero < minimo:
        return padrao
    return int(numero) if inteiro else float(numero)


def _config_screening(parametros: Any) -> tuple[dict[str, Any], str | None]:
    base = dict(estrategias.CONFIG_PADRAO["screening"])
    tipo = str(parametros.get("tipo") or base["tipo"]).strip().lower()
    if tipo in {"sma", "simples"}:
        base["tipo"] = "sma"
    elif tipo in {"ema", "exponencial"}:
        base["tipo"] = "ema"
    base["janela_cruzamento_dias"] = _numero_screening(
        parametros, "janela_cruzamento_dias", int(base["janela_cruzamento_dias"]),
        inteiro=True, minimo=1,
    )
    base["tolerancia_pullback_pct"] = _numero_screening(
        parametros, "tolerancia_pullback_pct", float(base["tolerancia_pullback_pct"]),
        minimo=0.0,
    )
    base["janela_pullback_dias"] = _numero_screening(
        parametros, "janela_pullback_dias", int(base["janela_pullback_dias"]),
        inteiro=True, minimo=1,
    )
    base["limiar_preco_m9_pct"] = _numero_screening(
        parametros, "limiar_preco_m9_pct", float(base["limiar_preco_m9_pct"]),
        minimo=0.0,
    )
    base["limiar_m9_m21_pct"] = _numero_screening(
        parametros, "limiar_m9_m21_pct", float(base["limiar_m9_m21_pct"]),
        minimo=0.0,
    )
    base["historico_minimo"] = _numero_screening(
        parametros, "historico_minimo", int(base["historico_minimo"]),
        inteiro=True, minimo=2,
    )
    classe = (parametros.get("classe") or "").strip() or None
    return base, classe


COLUNAS_SCREENING: list[dict[str, str]] = [
    {"chave": "ticker", "rotulo": "Ticker", "tipo": "texto"},
    {"chave": "classe", "rotulo": "Classe", "tipo": "texto"},
    {"chave": "preco", "rotulo": "Preço", "tipo": "moeda"},
    {"chave": "m9", "rotulo": "M9", "tipo": "moeda"},
    {"chave": "m21", "rotulo": "M21", "tipo": "moeda"},
    {"chave": "dist_preco_m9_pct", "rotulo": "Preço↔M9", "tipo": "percentual"},
    {"chave": "spread_m9_m21_pct", "rotulo": "M9↔M21", "tipo": "percentual"},
    {
        "chave": "dias_desde_cruzamento",
        "rotulo": "Dias desde o cruzamento",
        "tipo": "inteiro",
    },
    {"chave": "destaque", "rotulo": "Motivo / destaque", "tipo": "texto"},
]

SECOES_SCREENING: list[tuple[str, str]] = [
    ("cruzamento_alta", "Cruzamento de alta"),
    ("cruzamento_baixa", "Cruzamento de baixa"),
    ("alinhamento_alta", "Alinhamento de alta forte"),
    ("alinhamento_baixa", "Alinhamento de baixa forte"),
    ("pullback_alta", "Pullback na M9/M21"),
    ("esticado", "Esticados"),
]


def _destaque_screening(chave: str, item: dict[str, Any]) -> str | None:
    if chave in {"cruzamento_alta", "cruzamento_baixa"}:
        lado = "alta" if chave.endswith("alta") else "baixa"
        dias = item.get("dias_desde_cruzamento")
        if dias is None:
            return None
        return f"Cruzou para {lado} há {dias} pregão(ões)"
    if chave == "alinhamento_alta":
        return "Preço > M9 > M21 com as duas médias subindo"
    if chave == "alinhamento_baixa":
        return "Preço < M9 < M21 com as duas médias caindo"
    if chave == "pullback_alta":
        toque = item.get("pullback_alta") or {}
        if not toque:
            return None
        texto = (
            f"Tocou a {str(toque.get('media_tocada', '')).upper()} em "
            f"{formato.data_br(toque.get('data'))}"
        )
        if toque.get("candle_reversao"):
            texto += " com candle de reversão"
        return texto
    if chave == "esticado":
        return item.get("motivo_esticado")
    return None


def _linhas_screening(
    itens: list[dict[str, Any]], chave: str
) -> list[dict[str, dict[str, Any]]]:
    linhas: list[dict[str, dict[str, Any]]] = []
    for item in itens:
        destaque = _destaque_screening(chave, item)
        linhas.append(
            {
                "ticker": {
                    "texto": item.get("ticker"),
                    "motivo": None,
                    "link": f"/ativos/{item.get('ticker')}/tecnico",
                },
                "classe": {
                    "texto": consultas.nome_classe(item.get("classe")),
                    "motivo": None,
                },
                "preco": formato.celula(item.get("preco"), tipo="moeda"),
                "m9": formato.celula(item.get("m9"), tipo="moeda"),
                "m21": formato.celula(item.get("m21"), tipo="moeda"),
                "dist_preco_m9_pct": formato.celula(
                    item.get("dist_preco_m9_pct"), tipo="percentual", sinal=True
                ),
                "spread_m9_m21_pct": formato.celula(
                    item.get("spread_m9_m21_pct"), tipo="percentual", sinal=True
                ),
                "dias_desde_cruzamento": formato.celula(
                    item.get("dias_desde_cruzamento"),
                    tipo="inteiro",
                    motivo="sem cruzamento na janela",
                ),
                "destaque": {
                    "texto": destaque or "—",
                    "motivo": None,
                },
            }
        )
    return linhas


def _data_referencia_screening() -> str | None:
    with db.conectar() as conn:
        linha = conn.execute("SELECT MAX(data) AS data FROM cotacoes").fetchone()
    return linha["data"] if linha is not None else None


@app.get("/api/screening")
def api_screening(request: Request) -> dict[str, Any]:
    configuracao, classe = _config_screening(request.query_params)
    return screening.triar(configuracao, classe=classe)


@app.get("/api/rankings/{estrategia}")
def obter_ranking(estrategia: str) -> dict[str, Any]:
    with db.conectar() as conn:
        ranking = conn.execute(
            "SELECT * FROM rankings WHERE estrategia = ? ORDER BY id DESC LIMIT 1",
            (estrategia,),
        ).fetchone()
        if ranking is None:
            raise HTTPException(status_code=404, detail=f"ranking {estrategia} não encontrado")
        itens = conn.execute(
            """
            SELECT ticker, posicao, pontuacao, dados_json, motivos_json,
                   fora_da_lista
              FROM ranking_itens
             WHERE ranking_id = ?
             ORDER BY fora_da_lista, COALESCE(posicao, 999999), ticker
            """,
            (ranking["id"],),
        ).fetchall()

    try:
        config_ranking = json.loads(ranking["config_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        config_ranking = {}

    lista: list[dict[str, Any]] = []
    fora: list[dict[str, Any]] = []
    for item in itens:
        try:
            dados = json.loads(item["dados_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            dados = {}
        if item["fora_da_lista"]:
            entrada = {
                "ticker": item["ticker"],
                "motivo": dados.get("motivo", "inelegível"),
                "dados": dados.get("dados") or {},
            }
            fora.append(entrada)
            continue
        try:
            dados["motivos"] = json.loads(item["motivos_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            dados["motivos"] = {}
        dados["ticker"] = item["ticker"]
        dados["posicao"] = item["posicao"]
        dados["pontuacao"] = item["pontuacao"]
        lista.append(dados)

    return {
        "id": ranking["id"],
        "estrategia": ranking["estrategia"],
        "gerado_em": ranking["gerado_em"],
        "universo_n": ranking["universo_n"],
        "config": config_ranking,
        "itens": lista,
        "fora_da_lista": fora,
    }


def _executar_rankings(id_geracao: int) -> None:
    terminado = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        resumo = rankings.gerar_rankings()
        status = "ok"
        corpo = {
            nome: {
                "itens": len(dados["itens"]),
                "fora_da_lista": len(dados["fora_da_lista"]),
            }
            for nome, dados in resumo.items()
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("falha ao gerar rankings %s", id_geracao)
        status = "falha"
        corpo = {"erro": f"{type(exc).__name__}: {exc}"}
        db.registrar_falha("sistema", None, "rankings", corpo["erro"])

    with db.conectar() as conn:
        conn.execute(
            """
            UPDATE rankings_geracoes
               SET terminado_em = ?, status = ?, resumo_json = ?
             WHERE id = ?
            """,
            (terminado, status, json.dumps(corpo, ensure_ascii=False), id_geracao),
        )


@app.post("/api/rankings/gerar")
def gerar_rankings(background: BackgroundTasks) -> dict[str, Any]:
    inicio = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with db.conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO rankings_geracoes (iniciado_em, status) VALUES (?, ?)",
            (inicio, "em_andamento"),
        )
        id_geracao = int(cursor.lastrowid)
    background.add_task(_executar_rankings, id_geracao)
    return {"id": id_geracao}


@app.get("/api/ativos/{ticker}/tecnico")
def tecnico_ativo(ticker: str) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    df = _cotacoes_df(ticker)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"sem cotações para {ticker}")

    semanal = tecnicos.sma_semanal(df)
    benchmark = _cotacoes_df("^BVSP")
    return {
        "ticker": ticker,
        "data_ref": str(df.iloc[-1]["data"]),
        "medias_semanais": _ultimo_nao_nulo(semanal),
        "trend_template": tecnicos.trend_template(df),
        "forca_relativa": tecnicos.forca_relativa(df, benchmark)
        if not benchmark.empty
        else {
            f"{prefixo}_{rotulo}": None
            for rotulo in ("1m", "3m", "6m", "12m")
            for prefixo in ("ativo", "benchmark", "rs")
        },
        "sinais": tecnicos.sinais(df),
    }


@app.get("/api/ativos/{ticker}/exportacao")
def exportar_cotacoes(ticker: str) -> StreamingResponse:
    ticker = ticker.strip().upper()
    with db.conectar() as conn:
        existe = conn.execute(
            "SELECT 1 FROM cotacoes WHERE ticker = ? LIMIT 1", (ticker,)
        ).fetchone()
    if existe is None:
        raise HTTPException(status_code=404, detail=f"sem cotações para {ticker}")

    colunas = [
        "data",
        "abertura",
        "maxima",
        "minima",
        "fechamento",
        "fechamento_ajustado",
        "volume",
    ]

    def gerar() -> Iterator[str]:
        buffer = io.StringIO()
        escritor = csv.writer(buffer)
        escritor.writerow(colunas)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        with db.conectar() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(colunas)} FROM cotacoes WHERE ticker = ? ORDER BY data",
                (ticker,),
            )
            for linha in cursor:
                escritor.writerow([linha[coluna] for coluna in colunas])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    return StreamingResponse(
        gerar(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{ticker}.csv"'},
    )


ESTRATEGIAS_BACKTEST: list[tuple[str, str]] = [
    ("trend_template", "Trend Template"),
    ("forca_relativa", "Força relativa"),
    ("magic_formula", "Magic Formula"),
    ("graham", "Graham"),
    ("bazin", "Bazin"),
    ("dividendos", "Dividendos"),
    ("qualidade", "Qualidade e crescimento"),
]
TEMPO_LIMITE_BACKTEST = 20.0


def _valores_padrao_backtest() -> dict[str, str]:
    hoje = date.today()
    return {
        "estrategia": "trend_template",
        "inicio": (hoje - timedelta(days=730)).isoformat(),
        "fim": hoje.isoformat(),
        "rebalanceamento": "3",
        "top_n": "10",
        "custo": "10",
    }


def _argumentos_backtest(dados: Any) -> tuple[dict[str, str], dict[str, Any]]:
    valores = _valores_padrao_backtest()
    for chave in valores:
        if dados.get(chave) not in (None, ""):
            valores[chave] = str(dados.get(chave)).strip()

    try:
        inicio = date.fromisoformat(valores["inicio"][:10])
        fim = date.fromisoformat(valores["fim"][:10])
    except ValueError as exc:
        raise ValueError("datas inválidas: use AAAA-MM-DD") from exc
    try:
        rebalanceamento = int(valores["rebalanceamento"])
        top_n = int(valores["top_n"])
        custo = float(str(valores["custo"]).replace(",", "."))
    except ValueError as exc:
        raise ValueError("rebalanceamento, top_n e custo precisam ser numéricos") from exc

    nome = backtest._normalizar_estrategia(valores["estrategia"])
    if nome not in backtest.ESTRATEGIAS_FUNDAMENTALISTAS | backtest.ESTRATEGIAS_PRECO:
        raise ValueError(f"estratégia desconhecida para backtest: {valores['estrategia']}")
    valores["estrategia"] = nome

    argumentos = {
        "estrategia": nome,
        "inicio": inicio,
        "fim": fim,
        "rebalanceamento_meses": rebalanceamento,
        "top_n": top_n,
        "custo_bps": custo,
    }
    return valores, argumentos


async def _rodar_backtest(argumentos: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.wait_for(
        asyncio.to_thread(backtest.simular, **argumentos),
        timeout=TEMPO_LIMITE_BACKTEST,
    )


def _grafico_backtest(resultado: dict[str, Any]) -> dict[str, Any]:
    pontos_carteira = [
        {"time": ponto["data"], "value": float(ponto["carteira"])}
        for ponto in resultado.get("serie", [])
        if ponto.get("carteira") is not None
    ]
    pontos_benchmark = [
        {"time": ponto["data"], "value": float(ponto["benchmark"])}
        for ponto in resultado.get("serie", [])
        if ponto.get("benchmark") is not None
    ]
    series = [{"rotulo": "Carteira", "dados": pontos_carteira}]
    if pontos_benchmark:
        series.append(
            {
                "rotulo": resultado.get("benchmark", {}).get("ticker") or "Benchmark",
                "dados": pontos_benchmark,
            }
        )
    return {"tipo": "comparacao", "series": series}


def _contexto_backtest(
    *,
    valores: dict[str, str],
    resultado: dict[str, Any] | None = None,
    erro: str | None = None,
    aviso: str | None = None,
) -> dict[str, Any]:
    return {
        "valores": valores,
        "resultado": resultado,
        "grafico": _grafico_backtest(resultado) if resultado else None,
        "erro": erro,
        "aviso": aviso,
        "estrategias_disponiveis": ESTRATEGIAS_BACKTEST,
    }


@app.get("/backtests", response_class=HTMLResponse)
async def pagina_backtests(request: Request) -> HTMLResponse:
    valores = _valores_padrao_backtest()
    resultado = None
    erro = None
    aviso = None
    parametros = request.query_params
    if parametros.get("estrategia") and parametros.get("inicio") and parametros.get("fim"):
        try:
            valores, argumentos = _argumentos_backtest(parametros)
        except ValueError as exc:
            erro = str(exc)
        else:
            try:
                resultado = await _rodar_backtest(argumentos)
            except TimeoutError:
                aviso = "simulação acima de 20 s: reduza o período"
            except ValueError as exc:
                erro = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="backtests.html",
        context=_contexto(
            request,
            **_contexto_backtest(
                valores=valores, resultado=resultado, erro=erro, aviso=aviso
            ),
        ),
    )


@app.post("/backtests", response_class=HTMLResponse)
async def rodar_backtest(request: Request) -> HTMLResponse:
    # A página usa form urlencoded; ler o corpo direto evita depender de
    # python-multipart só para um formulário simples.
    bruto = (await request.body()).decode("utf-8", errors="replace")
    form = {
        chave: valores[-1]
        for chave, valores in parse_qs(bruto, keep_blank_values=True).items()
    }
    valores = _valores_padrao_backtest()
    resultado = None
    erro = None
    aviso = None
    try:
        valores, argumentos = _argumentos_backtest(form)
    except ValueError as exc:
        erro = str(exc)
    else:
        try:
            resultado = await _rodar_backtest(argumentos)
        except TimeoutError:
            aviso = "simulação acima de 20 s: reduza o período"
        except ValueError as exc:
            erro = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="backtests.html",
        context=_contexto(
            request,
            **_contexto_backtest(
                valores=valores, resultado=resultado, erro=erro, aviso=aviso
            ),
        ),
    )


@app.get("/api/backtests")
async def api_backtests(
    estrategia: str = Query(...),
    inicio: str = Query(...),
    fim: str = Query(...),
    rebalanceamento: int = Query(default=3),
    top_n: int = Query(default=10),
    custo: float = Query(default=10),
    rebalanceamento_meses: int | None = Query(default=None),
    custo_bps: float | None = Query(default=None),
) -> dict[str, Any]:
    dados = {
        "estrategia": estrategia,
        "inicio": inicio,
        "fim": fim,
        "rebalanceamento": rebalanceamento_meses
        if rebalanceamento_meses is not None
        else rebalanceamento,
        "top_n": top_n,
        "custo": custo_bps if custo_bps is not None else custo,
    }
    try:
        _, argumentos = _argumentos_backtest(dados)
        return await _rodar_backtest(argumentos)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="simulação acima de 20 s: reduza o período"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


ROTULOS_COLUNAS: dict[str, str] = {
    "ticker": "Ticker",
    "nome": "Nome",
    "classe": "Classe",
    "setor": "Setor",
    "posicao": "Pos.",
    "pontuacao": "Pontuação",
    "preco": "Preço",
    "variacao_pct": "Variação",
    "pl": "P/L",
    "pvp": "P/VP",
    "dy": "DY",
    "roe": "ROE",
    "roic": "ROIC",
    "margem_liquida": "Margem líquida",
    "divida_liquida_ebitda": "Dív. líq./EBITDA",
    "crescimento_receita_5a": "Cresc. receita 5a",
    "earnings_yield": "Earnings yield",
    "return_on_capital": "Retorno s/ capital",
    "ev_ebit": "EV/EBIT",
    "ebit_ev_pct": "EBIT/EV (%)",
    "ev_ebitda": "EV/EBITDA",
    "preco_teto": "Preço-teto",
    "dividendos_medios_por_acao": "Dividendos médios",
    "payout": "Payout",
    "liquidez_diaria_brl": "Liquidez/dia",
    "liquidez": "Liquidez",
    "retorno": "Retorno",
    "volatilidade": "Volatilidade",
    "sma50": "SMA 50 dias",
    "sma200": "SMA 200 dias",
    "regularidade": "Meses com provento (24)",
    "regularidade_24m_pct": "Regularidade (24m)",
    "regularidade_24m_meses": "Meses com provento (de 24)",
    "proventos_24m_qtd": "Pagamentos em 24m",
    "proventos_24m_observacao": "Observação",
    "anos_dividendos": "Anos de dividendos",
    "data_ref": "Data de referência",
    "ultima_data": "Última cotação",
    "fechamento": "Fechamento",
    "consenso": "Consenso",
    "alvo": "Alvo",
    "alvo_medio": "Alvo médio",
    "upside_pct": "Upside",
    "analistas": "Analistas (C/N/V)",
    "data_consenso": "Data do consenso",
    "retorno_total_12m_pct": "Retorno 12m",
    "valorizacao_12m_pct": "Cota 12m",
    "proventos_12m_pct": "Proventos 12m",
}

TIPOS_COLUNAS: dict[str, str] = {
    "ticker": "texto",
    "nome": "texto",
    "classe": "texto",
    "setor": "texto",
    "posicao": "inteiro",
    "anos_dividendos": "inteiro",
    "regularidade": "inteiro",
    "regularidade_24m_pct": "percentual",
    "regularidade_24m_meses": "inteiro",
    "proventos_24m_qtd": "inteiro",
    "proventos_24m_observacao": "texto",
    "data_ref": "data",
    "ultima_data": "data",
    "variacao_pct": "percentual",
    "dy": "percentual",
    "roe": "percentual",
    "roic": "percentual",
    "margem_liquida": "percentual",
    "crescimento_receita_5a": "percentual",
    "ebit_ev_pct": "percentual",
    "payout": "percentual",
    "retorno": "percentual",
    "volatilidade": "percentual",
    "preco": "moeda",
    "preco_teto": "moeda",
    "dividendos_medios_por_acao": "moeda",
    "liquidez_diaria_brl": "moeda",
    "liquidez": "moeda",
    "fechamento": "moeda",
    "consenso": "texto",
    "alvo": "numero",
    "alvo_medio": "moeda",
    "upside_pct": "percentual",
    "analistas": "texto",
    "data_consenso": "data",
    "retorno_total_12m_pct": "percentual",
    "valorizacao_12m_pct": "percentual",
    "proventos_12m_pct": "percentual",
}

MOTIVOS_COLUNAS: dict[str, str] = {
    "pl": "LPA menor ou igual a zero, ou ausente na fonte",
    "pvp": "VPA ausente ou não positivo",
    "dy": "sem provento por ação nos últimos 12 meses",
    "roe": "lucro ou patrimônio líquido ausente/não positivo",
    "roic": "capital investido ausente",
    "margem_liquida": "receita ou lucro ausentes",
    "divida_liquida_ebitda": "EBITDA ausente ou não positivo",
    "crescimento_receita_5a": "a fonte não fornece série plurianual de receita",
    "payout": "sem proventos ou LPA não positivo",
    "preco": "sem cotação na fonte",
    "fechamento": "sem cotação na fonte",
    "data_ref": "sem data de referência",
    "ultima_data": "sem cotação na fonte",
    "consenso": "sem consenso na fonte",
    "alvo": "sem preço-alvo na fonte",
    "alvo_medio": "sem preço-alvo na fonte",
    "upside_pct": "sem upside calculado para o ativo",
    "analistas": "sem consenso na fonte",
    "data_consenso": "sem data do consenso na fonte",
    "retorno_total_12m_pct": "sem cotação de 12 meses atrás",
    "valorizacao_12m_pct": "sem cotação de 12 meses atrás",
    "proventos_12m_pct": "sem provento nos últimos 12 meses",
    "regularidade_24m_pct": "sem provento nos últimos 24 meses",
    "regularidade_24m_meses": "sem provento nos últimos 24 meses",
    "proventos_24m_qtd": "sem provento nos últimos 24 meses",
    "proventos_24m_observacao": "sem provento nos últimos 24 meses",
}


def _definicao_coluna(chave: str) -> dict[str, Any]:
    definicao: dict[str, Any] = {
        "chave": chave,
        "rotulo": ROTULOS_COLUNAS.get(chave, chave.replace("_", " ").title()),
        "tipo": TIPOS_COLUNAS.get(chave, "numero"),
    }
    if chave in {
        "retorno_total_12m_pct",
        "valorizacao_12m_pct",
        "proventos_12m_pct",
    }:
        definicao["sinal"] = True
    return definicao


def _colunas_pedidas(valor: str | None, padrao: list[str]) -> list[dict[str, str]]:
    if not valor:
        return [_definicao_coluna(chave) for chave in padrao]
    pedidas = [parte.strip() for parte in valor.split(",") if parte.strip()]
    validas = [chave for chave in pedidas if chave in ROTULOS_COLUNAS]
    if not validas:
        validas = padrao
    return [_definicao_coluna(chave) for chave in validas]


def _ordenar_itens(
    itens: list[dict[str, Any]], coluna: dict[str, str], direcao: str
) -> list[dict[str, Any]]:
    chave = coluna["chave"]
    tipo = coluna["tipo"]

    def valor_texto(item: dict[str, Any]) -> str:
        valor = consultas.valor_item(item, chave)
        return "" if valor is None else str(valor).lower()

    def valor_numero(item: dict[str, Any]) -> float | None:
        return formato.converter_numero(consultas.valor_item(item, chave))

    if tipo in {"texto", "data"}:
        presentes = [item for item in itens if valor_texto(item)]
        ausentes = [item for item in itens if not valor_texto(item)]
        presentes.sort(key=valor_texto, reverse=direcao == "desc")
    else:
        presentes = [item for item in itens if valor_numero(item) is not None]
        ausentes = [item for item in itens if valor_numero(item) is None]
        presentes.sort(
            key=lambda item: valor_numero(item) or 0.0,
            reverse=direcao == "desc",
        )
    ausentes.sort(key=lambda item: str(item.get("ticker") or ""))
    return presentes + ausentes


def _colunas_com_links(
    colunas: list[dict[str, str]],
    ordem: str,
    direcao: str,
    endpoint: str,
    parametros: dict[str, Any],
) -> list[dict[str, str]]:
    resultado = []
    for coluna in colunas:
        if coluna["chave"] == ordem:
            proxima = "asc" if direcao == "desc" else "desc"
        else:
            proxima = "asc"
        consulta = {chave: valor for chave, valor in parametros.items() if valor}
        consulta.update({"ordem": coluna["chave"], "dir": proxima})
        resultado.append(
            {
                **coluna,
                "link": f"{endpoint}?{urlencode(consulta)}",
                "ordenado": coluna["chave"] == ordem,
                "direcao": direcao if coluna["chave"] == ordem else "none",
            }
        )
    return resultado


def _titulo_consenso(resumo: dict[str, Any], base: str) -> str:
    partes = [base]
    if resumo.get("alvo_classe_duvidosa"):
        partes.append(
            "classe em dúvida "
            "(o alvo pode ser da unit/outra classe do papel)"
        )
    if resumo.get("data"):
        partes.append(f"fonte atualizada em {resumo['data']}")
    return " · ".join(partes)


def _celula_consenso(item: dict[str, Any]) -> dict[str, Any]:
    total = consultas.valor_item(item, "consenso_total")
    resumo = consultas.consenso_resumo(item)
    if resumo is None:
        motivo = (
            "sem contagem por analista na fonte"
            if total is not None
            else "sem consenso na fonte"
        )
        return formato.celula(None, tipo="texto", motivo=motivo)
    return {
        "texto": resumo["texto"],
        "motivo": None,
        "titulo": _titulo_consenso(
            resumo, "C = compra, N = neutro, V = venda"
        ),
    }


def _celula_alvo(item: dict[str, Any]) -> dict[str, Any]:
    alvo = consultas.valor_item(item, "alvo_medio")
    if alvo is None:
        return formato.celula(
            None, tipo="moeda", motivo="sem preço-alvo na fonte"
        )
    resumo = consultas.consenso_resumo(item)
    if resumo is not None and resumo.get("alvo_texto"):
        return {
            "texto": resumo["alvo_texto"],
            "motivo": None,
            "titulo": _titulo_consenso(resumo, "Preço-alvo médio do consenso MSN"),
        }
    preco = consultas.valor_item(item, "preco")
    if preco is None:
        preco = consultas.valor_item(item, "fechamento")
    texto = formato.moeda(alvo)
    if msn.alvo_classe_duvidosa(alvo, preco):
        texto = (
            f"{texto} · classe em dúvida "
            "(o alvo pode ser da unit/outra classe do papel)"
        )
    else:
        upside = msn.upside_pct(alvo, preco)
        if upside is not None:
            texto = (
                f"{texto} · {formato.percentual(upside, casas=1, sinal=True)}"
            )
    return {"texto": texto, "motivo": None}


def _celula_item(item: dict[str, Any], coluna: dict[str, str]) -> dict[str, Any]:
    chave = coluna["chave"]
    valor = consultas.valor_item(item, chave)
    if chave in {"consenso", "analistas"}:
        return _celula_consenso(item)
    if chave == "alvo":
        return _celula_alvo(item)
    if chave == "ticker" and valor:
        return {
            "texto": str(valor),
            "motivo": None,
            "link": f"/ativos/{str(valor).upper()}",
        }
    celula: dict[str, Any] = formato.celula(
        valor,
        tipo=coluna["tipo"],
        motivo=MOTIVOS_COLUNAS.get(chave),
        sinal=chave in {"variacao_pct", "upside_pct"} or bool(coluna.get("sinal")),
    )
    # DY é o número de decisão do dono: sai destacado em toda tabela.
    if chave == "dy":
        celula["destaque"] = True
    return celula


def _linhas_tabela(
    itens: list[dict[str, Any]], colunas: list[dict[str, str]]
) -> list[dict[str, Any]]:
    return [
        {coluna["chave"]: _celula_item(item, coluna) for coluna in colunas}
        for item in itens
    ]


def _contexto(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, **extra}


def _min_listas(valor: Any, padrao: int = 3) -> int:
    if valor in (None, ""):
        return padrao
    try:
        return max(1, int(str(valor).strip()))
    except (TypeError, ValueError):
        return padrao


def _consolidado(min_listas: int, classe: str | None) -> list[dict[str, Any]]:
    itens = consolidado.consolidar({"min_listas": min_listas})
    if classe:
        itens = [item for item in itens if item.get("classe") == classe]
    return itens


def _onde_aparece(item: dict[str, Any]) -> list[dict[str, Any]]:
    onde = []
    for estrategia, posicao in (item.get("posicoes") or {}).items():
        onde.append(
            {
                "estrategia": estrategia,
                "nome": consultas.nome_curto_estrategia(estrategia),
                "posicao": posicao["posicao"],
                "de": posicao["de"],
                "link": f"/rankings/{estrategia}",
            }
        )
    return onde


def _linha_upside(item: dict[str, Any]) -> dict[str, Any]:
    dados = item.get("dados") or {}
    return {
        "preco": dados.get("preco"),
        "alvo": dados.get("alvo_medio"),
        "upside_pct": dados.get("upside_pct"),
        "analistas": {
            "compra": dados.get("consenso_compra"),
            "neutro": dados.get("consenso_neutro"),
            "venda": dados.get("consenso_venda"),
            "total": dados.get("consenso_total"),
        },
        "data_consenso": formato.data_br(dados.get("data_consenso")),
    }


def _data_consenso_secao(itens: list[dict[str, Any]]) -> str | None:
    datas = [
        str((item.get("dados") or {}).get("data_consenso") or "")[:10]
        for item in itens
    ]
    datas = [data for data in datas if data]
    return formato.data_br(max(datas)) if datas else None


@app.get("/", response_class=HTMLResponse)
def pagina_dashboard(request: Request) -> HTMLResponse:
    resumo = consultas.resumo_dashboard()
    rankings_topo = []
    for recente in consultas.rankings_recentes():
        if recente["id"] is None:
            continue
        ranking = consultas.carregar_ranking(recente["estrategia"])
        if ranking is None:
            continue
        itens = ranking["itens"][:5]
        if recente["estrategia"] == "upside_consenso":
            itens = [
                {**item, "linha_upside": _linha_upside(item)} for item in itens
            ]
        rankings_topo.append(
            {
                "estrategia": recente["estrategia"],
                "nome": consultas.nome_estrategia(recente["estrategia"]),
                "gerado_em": recente["gerado_em"],
                "itens": itens,
                "consenso_data": _data_consenso_secao(itens),
            }
        )
    min_listas = int(consolidado.CONFIG_PADRAO["min_listas"])
    consolidado_topo = _consolidado(min_listas, None)[:10]
    for item in consolidado_topo:
        item["onde_aparece"] = _onde_aparece(item)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_contexto(
            request,
            resumo=resumo,
            rankings=rankings_topo,
            consolidado=consolidado_topo,
            consolidado_min_listas=min_listas,
            alertas=consultas.ultimos_alertas(),
        ),
    )


@app.get("/rankings", response_class=HTMLResponse)
def pagina_rankings(request: Request) -> HTMLResponse:
    ordem = request.query_params.get("ordem") or "posicao"
    direcao = request.query_params.get("dir") or "asc"
    direcao = "desc" if direcao.lower() == "desc" else "asc"
    cols_param = request.query_params.get("cols")
    colunas_base = _colunas_pedidas(
        cols_param, ["posicao", "ticker", "pl", "pvp", "dy", "pontuacao"]
    )
    if ordem not in {coluna["chave"] for coluna in colunas_base}:
        ordem = "posicao" if any(
            coluna["chave"] == "posicao" for coluna in colunas_base
        ) else colunas_base[0]["chave"]
    coluna_atual = _definicao_coluna(ordem)
    colunas = _colunas_com_links(
        colunas_base,
        ordem,
        direcao,
        "/rankings",
        {"cols": cols_param} if cols_param else {},
    )

    secoes = []
    for recente in consultas.rankings_recentes():
        ranking = consultas.carregar_ranking(recente["estrategia"])
        itens = []
        if ranking is not None:
            itens = _ordenar_itens(ranking["itens"][:10], coluna_atual, direcao)
        colunas_secao = colunas
        if not cols_param and recente["estrategia"] == "magic_formula":
            colunas_secao = _colunas_com_links(
                [
                    _definicao_coluna(chave)
                    for chave in (
                        "posicao",
                        "ticker",
                        "earnings_yield",
                        "return_on_capital",
                        "ev_ebit",
                        "ebit_ev_pct",
                        "pontuacao",
                    )
                ],
                ordem,
                direcao,
                "/rankings",
                {},
            )
        if not cols_param and recente["estrategia"] == "upside_consenso":
            colunas_secao = _colunas_com_links(
                [
                    _definicao_coluna(chave)
                    for chave in (
                        "posicao",
                        "ticker",
                        "preco",
                        "alvo_medio",
                        "upside_pct",
                        "dy",
                        "analistas",
                        "data_consenso",
                    )
                ],
                ordem,
                direcao,
                "/rankings",
                {},
            )
        if not cols_param and recente["estrategia"] == "regularidade_proventos":
            colunas_secao = _colunas_com_links(
                [
                    _definicao_coluna(chave)
                    for chave in (
                        "posicao",
                        "ticker",
                        "regularidade_24m_pct",
                        "regularidade_24m_meses",
                        "proventos_24m_qtd",
                        "dy",
                        "proventos_24m_observacao",
                    )
                ],
                ordem,
                direcao,
                "/rankings",
                {},
            )
        if not cols_param and recente["estrategia"] == "fiis_ltm":
            colunas_secao = _colunas_com_links(
                [
                    _definicao_coluna(chave)
                    for chave in (
                        "posicao",
                        "ticker",
                        "retorno_total_12m_pct",
                        "valorizacao_12m_pct",
                        "proventos_12m_pct",
                        "dy",
                    )
                ],
                ordem,
                direcao,
                "/rankings",
                {},
            )
        secoes.append(
            {
                "estrategia": recente["estrategia"],
                "nome": consultas.nome_estrategia(recente["estrategia"]),
                "gerado_em": recente["gerado_em"],
                "existe": ranking is not None,
                "total": len(ranking["itens"]) if ranking else 0,
                "colunas": colunas_secao,
                "linhas": _linhas_tabela(itens, colunas_secao),
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="rankings.html",
        context=_contexto(
            request,
            secoes=secoes,
            colunas=colunas,
            ordem=ordem,
            direcao=direcao,
            cols_param=cols_param,
        ),
    )


@app.get("/rankings/consolidado", response_class=HTMLResponse)
def pagina_ranking_consolidado(request: Request) -> HTMLResponse:
    min_listas = _min_listas(request.query_params.get("min_listas"))
    classe = request.query_params.get("classe") or None
    itens = _consolidado(min_listas, classe)
    for item in itens:
        item["onde_aparece"] = _onde_aparece(item)
    return templates.TemplateResponse(
        request=request,
        name="consolidado.html",
        context=_contexto(
            request,
            itens=itens,
            estrategias=rankings.estrategias_ativas(),
            nomes_curtos={
                e: consultas.nome_curto_estrategia(e)
                for e in rankings.estrategias_ativas()
            },
            min_listas=min_listas,
            classe=classe,
            opcoes_classes=consultas.CLASSES,
            gerado_em=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        ),
    )


@app.get("/rankings/{estrategia}", response_class=HTMLResponse)
def pagina_ranking(request: Request, estrategia: str) -> HTMLResponse:
    alias = rankings.ALIASES_CLASSE.get(str(estrategia or "").strip().lower())
    if alias:
        return RedirectResponse(url=f"/rankings/{alias}", status_code=302)
    ranking = consultas.carregar_ranking(estrategia)
    if ranking is None:
        raise HTTPException(
            status_code=404, detail=f"ranking {estrategia} não encontrado"
        )
    ordem = request.query_params.get("ordem") or "posicao"
    direcao = request.query_params.get("dir") or "asc"
    direcao = "desc" if direcao.lower() == "desc" else "asc"
    cols_param = request.query_params.get("cols")
    padrao = ["posicao", "ticker", "preco", "pl", "pvp", "dy", "pontuacao"]
    if estrategia == "fiis_ltm":
        padrao = [
            "posicao",
            "ticker",
            "retorno_total_12m_pct",
            "valorizacao_12m_pct",
            "proventos_12m_pct",
            "dy",
        ]
    if estrategia == "regularidade_proventos":
        padrao = [
            "posicao",
            "ticker",
            "regularidade_24m_pct",
            "regularidade_24m_meses",
            "proventos_24m_qtd",
            "dy",
            "proventos_24m_observacao",
        ]
    colunas_base = _colunas_pedidas(cols_param, padrao)
    if ordem not in {coluna["chave"] for coluna in colunas_base}:
        ordem = "posicao" if any(
            coluna["chave"] == "posicao" for coluna in colunas_base
        ) else colunas_base[0]["chave"]
    colunas = _colunas_com_links(
        colunas_base,
        ordem,
        direcao,
        f"/rankings/{estrategia}",
        {"cols": cols_param} if cols_param else {},
    )
    itens = _ordenar_itens(
        ranking["itens"], _definicao_coluna(ordem), direcao
    )
    for item in itens:
        item["consenso_resumo"] = consultas.consenso_resumo(item)
    resumos = [item["consenso_resumo"] for item in itens if item["consenso_resumo"]]
    tem_consenso = bool(resumos) or any(
        (formato.converter_numero(consultas.valor_item(item, "consenso_total")) or 0)
        > 0
        for item in itens
    )
    datas_consenso = [
        str(
            consultas.valor_item(item, "consenso_data_ref")
            or consultas.valor_item(item, "data_ref")
            or ""
        )[:10]
        for item in itens
        if item["consenso_resumo"]
    ]
    datas_consenso = [data for data in datas_consenso if data]
    return templates.TemplateResponse(
        request=request,
        name="ranking.html",
        context=_contexto(
            request,
            ranking=ranking,
            nome=consultas.nome_estrategia(estrategia),
            colunas=colunas,
            linhas=_linhas_tabela(itens, colunas),
            itens=itens,
            ordem=ordem,
            direcao=direcao,
            cols_param=cols_param,
            tem_consenso=tem_consenso,
            consenso_data=(
                formato.data_br(max(datas_consenso)) if datas_consenso else None
            ),
        ),
    )


@app.get("/screening", response_class=HTMLResponse)
def pagina_screening(request: Request) -> HTMLResponse:
    configuracao, classe = _config_screening(request.query_params)
    listas = screening.triar(configuracao, classe=classe)

    tipo = configuracao["tipo"]
    rotulo_tipo = (
        "EMA (exponencial, alpha = 2/(n+1))"
        if tipo == "ema"
        else "SMA (simples)"
    )
    definicoes = [
        (
            "Médias",
            f"{rotulo_tipo} de 9 e 21 sobre o fechamento diário, "
            "sem usar o fechamento ajustado.",
        ),
        (
            "Cruzamento",
            "Última troca de sinal de M9 − M21 nos últimos "
            f"{configuracao['janela_cruzamento_dias']} pregões. Alta quando a "
            "diferença passa de ≤ 0 para > 0; baixa no sentido inverso.",
        ),
        (
            "Alinhamento forte",
            "Alta: preço > M9 > M21 e as duas médias subindo em relação a 5 "
            "pregões atrás. Baixa: preço < M9 < M21 e as duas caindo.",
        ),
        (
            "Pullback",
            "Com M9 > M21, candle dos últimos "
            f"{configuracao['janela_pullback_dias']} pregões cuja mínima tocou "
            f"a M9 ou a M21 (tolerância de {configuracao['tolerancia_pullback_pct']:g}%) "
            "e cujo fechamento ficou acima da média tocada.",
        ),
        (
            "Esticado",
            "Preço a pelo menos "
            f"{configuracao['limiar_preco_m9_pct']:g}% acima da M9 ou spread "
            f"M9↔M21 de pelo menos {configuracao['limiar_m9_m21_pct']:g}%.",
        ),
        (
            "Histórico",
            f"Mínimo de {configuracao['historico_minimo']} barras diárias. "
            "Abaixo disso o ativo fica indisponível, nunca aparece como "
            "“sem sinal”.",
        ),
    ]

    secoes = []
    for chave, titulo in SECOES_SCREENING:
        itens = listas.get(chave, [])
        secoes.append(
            {
                "chave": chave,
                "titulo": titulo,
                "total": len(itens),
                "linhas": _linhas_screening(itens, chave),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="screening.html",
        context=_contexto(
            request,
            configuracao=configuracao,
            classe=classe,
            opcoes_classes=consultas.CLASSES,
            definicoes=definicoes,
            secoes=secoes,
            colunas=COLUNAS_SCREENING,
            data_ref=formato.data_br(_data_referencia_screening()),
        ),
    )


CLASSES_UNIVERSO = ("acao_b3", "bdr", "etf", "fii", "cripto")


def _redirecionar_universo(aviso: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/universo?{urlencode({'aviso': aviso})}", status_code=303
    )


def _itens_universo() -> tuple[list[dict[str, Any]], dict[str, int]]:
    lista = universo.ler()
    banco = {item["ticker"]: item for item in db.ativos_com_status()}

    contagens = {classe: 0 for classe in CLASSES_UNIVERSO}
    itens: list[dict[str, Any]] = []
    for ticker, info in lista.items():
        classe = info.get("classe")
        if classe in contagens:
            contagens[classe] += 1
        dados = banco.get(ticker, {})
        itens.append(
            {
                "ticker": ticker,
                "nome": dados.get("nome"),
                "classe": classe,
                "classe_nome": consultas.nome_classe(classe),
                "setor": dados.get("setor"),
                "ultima_data": dados.get("ultima_data"),
                "ultimo_fechamento": dados.get("ultimo_fechamento"),
                "na_lista": True,
                "status": "na_lista",
                "status_texto": "Na lista",
                "pode_reincluir": False,
            }
        )

    for ticker, dados in banco.items():
        if ticker in lista:
            continue
        classe = dados.get("classe")
        itens.append(
            {
                "ticker": ticker,
                "nome": dados.get("nome"),
                "classe": classe,
                "classe_nome": consultas.nome_classe(classe),
                "setor": dados.get("setor"),
                "ultima_data": dados.get("ultima_data"),
                "ultimo_fechamento": dados.get("ultimo_fechamento"),
                "na_lista": False,
                "status": "fora",
                "status_texto": "Fora da lista",
                "pode_reincluir": classe in CLASSES_UNIVERSO,
            }
        )

    ordem = {classe: posicao for posicao, classe in enumerate(CLASSES_UNIVERSO)}
    itens.sort(key=lambda item: (ordem.get(item["classe"], 99), item["ticker"]))
    return itens, contagens


@app.get("/universo", response_class=HTMLResponse)
def pagina_universo(request: Request) -> HTMLResponse:
    classe = request.query_params.get("classe") or None
    status = request.query_params.get("status") or "todos"
    busca = (request.query_params.get("busca") or "").strip() or None

    itens, contagens = _itens_universo()
    filtrados = itens
    if classe:
        filtrados = [item for item in filtrados if item["classe"] == classe]
    if status in {"na_lista", "lista"}:
        filtrados = [item for item in filtrados if item["na_lista"]]
    elif status in {"fora", "fora_da_lista"}:
        filtrados = [item for item in filtrados if not item["na_lista"]]
    if busca:
        termo = busca.casefold()
        filtrados = [
            item
            for item in filtrados
            if termo in item["ticker"].casefold()
            or termo in str(item.get("nome") or "").casefold()
        ]

    cartoes = [
        {
            "classe": chave,
            "nome": consultas.nome_classe(chave),
            "total": contagens.get(chave, 0),
        }
        for chave in CLASSES_UNIVERSO
    ]
    total_lista = sum(contagens.values())

    return templates.TemplateResponse(
        request=request,
        name="universo.html",
        context=_contexto(
            request,
            ativos=filtrados,
            cartoes=cartoes,
            total_lista=total_lista,
            filtros={"classe": classe, "status": status, "busca": busca},
            opcoes_classes=[
                {"chave": chave, "nome": consultas.nome_classe(chave)}
                for chave in CLASSES_UNIVERSO
            ],
            aviso=request.query_params.get("aviso"),
        ),
    )


@app.post("/universo/incluir")
async def incluir_no_universo(
    request: Request, background: BackgroundTasks
) -> RedirectResponse:
    form = await request.form()
    ticker = str(form.get("ticker") or "")
    classe = str(form.get("classe") or "")
    try:
        resultado = universo.incluir(ticker, classe)
    except ValueError as exc:
        mensagem = str(exc)
        if "já está na lista" in mensagem:
            mensagem = f"{ticker.strip().upper()} já estava na lista"
        return _redirecionar_universo(mensagem)
    except (FileNotFoundError, OSError) as exc:
        return _redirecionar_universo(str(exc))

    normalizado = resultado["ticker"]
    db.upsert_ativo(normalizado, classe=resultado["classe"])
    db.marcar_ativo(normalizado, True)

    id_coleta = db.abrir_coleta_ou_recusar("universo")
    if id_coleta is None:
        aviso = (
            f"{normalizado} incluído; a coleta ficou para depois "
            "(já havia coleta em andamento)"
        )
    else:
        background.add_task(
            _executar_coleta, id_coleta, [normalizado], config.PERIODO_PADRAO
        )
        aviso = f"{normalizado} incluído e coleta iniciada"
    return _redirecionar_universo(aviso)


@app.post("/universo/excluir")
async def excluir_do_universo(request: Request) -> RedirectResponse:
    form = await request.form()
    ticker = str(form.get("ticker") or "")
    try:
        resultado = universo.excluir(ticker)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return _redirecionar_universo(str(exc))

    db.marcar_ativo(resultado["ticker"], False)
    return _redirecionar_universo(f"{resultado['ticker']} removido da lista")


@app.post("/universo/reincluir")
async def reincluir_no_universo(request: Request) -> RedirectResponse:
    form = await request.form()
    ticker = str(form.get("ticker") or "").strip().upper()
    classe = str(form.get("classe") or "").strip() or None
    if classe is None:
        with db.conectar() as conn:
            linha = conn.execute(
                "SELECT classe FROM ativos WHERE ticker = ?", (ticker,)
            ).fetchone()
        classe = linha["classe"] if linha is not None else None
    if not classe:
        return _redirecionar_universo(
            f"{ticker} não tem classe conhecida para reincluir"
        )

    try:
        resultado = universo.incluir(ticker, classe)
    except ValueError as exc:
        mensagem = str(exc)
        if "já está na lista" in mensagem:
            db.marcar_ativo(ticker, True)
            return _redirecionar_universo(f"{ticker} já estava na lista")
        return _redirecionar_universo(mensagem)
    except (FileNotFoundError, OSError) as exc:
        return _redirecionar_universo(str(exc))

    db.marcar_ativo(resultado["ticker"], True)
    return _redirecionar_universo(f"{resultado['ticker']} reincluído na lista")


@app.get("/ativos", response_class=HTMLResponse)
def pagina_ativos(request: Request) -> HTMLResponse:
    classe = request.query_params.get("classe") or None
    setor = request.query_params.get("setor") or None
    busca = request.query_params.get("busca") or None
    ordem = request.query_params.get("ordem") or "ticker"
    direcao = request.query_params.get("dir") or "asc"
    direcao = "desc" if direcao.lower() == "desc" else "asc"
    cols_param = request.query_params.get("cols")
    try:
        pagina = max(1, int(request.query_params.get("pagina") or 1))
    except ValueError:
        pagina = 1

    colunas_base = _colunas_pedidas(
        cols_param,
        [
            "ticker",
            "nome",
            "classe",
            "setor",
            "ultima_data",
            "fechamento",
            "dy",
            "variacao_pct",
        ],
    )
    if ordem not in {coluna["chave"] for coluna in colunas_base}:
        ordem = (
            "ticker"
            if any(coluna["chave"] == "ticker" for coluna in colunas_base)
            else colunas_base[0]["chave"]
        )

    dados = consultas.listar_ativos(
        classe=classe,
        setor=setor,
        busca=busca,
        ordem=ordem,
        direcao=direcao,
        pagina=pagina,
    )
    colunas = _colunas_com_links(
        colunas_base,
        ordem,
        direcao,
        "/ativos",
        {
            "classe": classe,
            "setor": setor,
            "busca": busca,
            "cols": cols_param,
            "pagina": pagina,
        },
    )
    filtros = {"classe": classe, "setor": setor, "busca": busca}

    def link_pagina(destino: int) -> str:
        parametros = {
            chave: valor
            for chave, valor in {
                **filtros,
                "cols": cols_param,
                "ordem": ordem,
                "dir": direcao,
                "pagina": destino,
            }.items()
            if valor
        }
        return f"/ativos?{urlencode(parametros)}"

    return templates.TemplateResponse(
        request=request,
        name="ativos.html",
        context=_contexto(
            request,
            dados=dados,
            opcoes=consultas.opcoes_filtros(),
            filtros=filtros,
            colunas=colunas,
            linhas=_linhas_tabela(dados["itens"], colunas),
            ordem=ordem,
            direcao=direcao,
            link_anterior=link_pagina(pagina - 1) if pagina > 1 else None,
            link_proxima=link_pagina(pagina + 1)
            if pagina < dados["paginas"]
            else None,
        ),
    )


INDICADORES_DEMONSTRACOES: list[tuple[str, str, str]] = [
    ("pl", "P/L", "numero"),
    ("pvp", "P/VP", "numero"),
    ("roe", "ROE", "percentual"),
    ("roic", "ROIC", "percentual"),
    ("margem_liquida", "Margem líquida", "percentual"),
    ("divida_liquida_ebitda", "Dívida líquida/EBITDA", "numero"),
    ("ev_ebitda", "EV/EBITDA", "numero"),
    ("ebitda", "EBITDA", "moeda"),
    ("valor_firma", "Valor da firma", "moeda"),
    ("lpa", "LPA", "moeda"),
    ("vpa", "VPA", "moeda"),
    ("crescimento_receita_5a", "Crescimento de receita 5a", "percentual"),
]

INDICADORES_PROVENTOS: list[tuple[str, str, str]] = [
    ("proventos_12m", "Proventos por ação (12m)", "moeda"),
    ("dy_12m_calculado", "Dividend yield 12m calculado", "percentual"),
    ("payout", "Payout", "percentual"),
]

INDICADORES_RESUMO_EXTRA: list[tuple[str, str, str]] = [
    ("pvp", "P/VP", "numero"),
    ("dy", "Dividend yield informado", "percentual"),
    ("ev_ebitda_calculado", "EV/EBITDA calculado", "numero"),
]


def _linha_indicador(
    valor: Any, rotulo: str, tipo: str, motivo: str | None = None
) -> dict[str, Any]:
    celula = formato.celula(valor, tipo=tipo, motivo=motivo)
    return {
        "rotulo": rotulo,
        "valor": celula["texto"],
        "motivo": celula["motivo"],
    }


CAMPOS_DETALHE = {"ebitda": "ev_ebitda"}


def _detalhe_do_indicador(
    chave: str, detalhes: dict[str, dict[str, Any]], indicadores: dict[str, Any]
) -> dict[str, Any] | None:
    if chave == "pvp":
        origem = indicadores.get("pvp_origem")
        mapa = {
            "fundamentus": "pvp",
            "yahoo": "pvp_yahoo",
            "cvm": "vpa_cvm",
            "calculado": "vpa_calculado",
        }
        return detalhes.get(mapa.get(str(origem), "pvp"))
    if chave == "vpa":
        return detalhes.get("vpa_calculado") or detalhes.get("vpa_cvm")
    return detalhes.get(CAMPOS_DETALHE.get(chave, chave))


def _linha_fundamento(
    chave: str,
    valor: Any,
    rotulo: str,
    tipo: str,
    motivo: str | None,
    detalhes: dict[str, dict[str, Any]],
    data_padrao: str | None,
    indicadores: dict[str, Any],
) -> dict[str, Any]:
    info = _detalhe_do_indicador(chave, detalhes, indicadores)
    celula = formato.celula(valor, tipo=tipo, motivo=motivo)
    motivo_final = celula["motivo"]
    if motivo_final:
        if info is not None and info.get("data_ref"):
            motivo_final = motivo or (
                f"último valor de {formato.data_br(info['data_ref'])}"
            )
        else:
            motivo_final = motivo or "fonte não entrega"
    fonte = info.get("fonte") if info is not None else None
    if chave == "pvp" and indicadores.get("pvp_origem"):
        fonte = indicadores["pvp_origem"]
    if fonte is None and motivo_final is None:
        fonte = "calculado"
    return {
        "rotulo": rotulo,
        "valor": celula["texto"],
        "motivo": motivo_final,
        "fonte": fonte,
        "data_ref": (
            formato.data_br(info["data_ref"]) if info and info.get("data_ref") else data_padrao
        ),
    }


def _contexto_tecnico(dados: dict[str, Any], periodo: str) -> dict[str, Any]:
    if periodo not in {"diario", "semanal", "mensal"}:
        periodo = "diario"
    cotacoes = dados["cotacoes"]
    df = pd.DataFrame(cotacoes)
    if df.empty:
        return {
            "periodo": periodo,
            "sem_dados": True,
            "grafico": {"tipo": "candles", "candles": [], "volumes": [], "medias": []},
        }

    df["data"] = pd.to_datetime(df["data"], errors="coerce")
    df = df.dropna(subset=["data"]).sort_values("data").reset_index(drop=True)

    if periodo == "semanal":
        trabalho = tecnicos.resumo_semanal(df).reset_index()
        medias = tecnicos.sma_semanal(df, (9, 21, 100, 150))
        rotulos_medias = {
            "sma9s": "9 semanas (45 pregões)",
            "sma21s": "21 semanas (105 pregões)",
            "sma100s": "100 semanas (500 pregões)",
            "sma150s": "150 semanas (750 pregões)",
        }
    elif periodo == "mensal":
        base = df.copy().set_index("data").sort_index()
        try:
            mensal = base.resample("ME").agg(
                {
                    "abertura": "first",
                    "maxima": "max",
                    "minima": "min",
                    "fechamento": "last",
                    "volume": "sum",
                }
            )
        except ValueError:  # pragma: no cover - pandas antigo
            mensal = base.resample("M").agg(
                {
                    "abertura": "first",
                    "maxima": "max",
                    "minima": "min",
                    "fechamento": "last",
                    "volume": "sum",
                }
            )
        mensal = mensal.dropna(subset=["fechamento"])
        trabalho = mensal.reset_index()
        medias = pd.DataFrame(index=mensal.index)
        for janela in (9, 21, 50):
            medias[f"sma{janela}m"] = mensal["fechamento"].rolling(
                janela, min_periods=janela
            ).mean()
        rotulos_medias = {
            "sma9m": "9 meses",
            "sma21m": "21 meses",
            "sma50m": "50 meses",
        }
    else:
        trabalho = df.copy()
        medias = tecnicos.medias_moveis(df, (50, 150, 200))
        rotulos_medias = {
            "sma50": "SMA 50 dias",
            "sma150": "SMA 150 dias",
            "sma200": "SMA 200 dias",
        }

    if "data" not in trabalho.columns:
        trabalho = trabalho.reset_index()
    trabalho["data"] = pd.to_datetime(trabalho["data"], errors="coerce")
    trabalho = trabalho.dropna(subset=["data"])

    candles = []
    volumes = []
    for _, linha in trabalho.iterrows():
        data_iso = linha["data"].strftime("%Y-%m-%d")
        candles.append(
            {
                "time": data_iso,
                "open": _float(linha.get("abertura")),
                "high": _float(linha.get("maxima")),
                "low": _float(linha.get("minima")),
                "close": _float(linha.get("fechamento")),
            }
        )
        volumes.append(
            {
                "time": data_iso,
                "value": _float(linha.get("volume")) or 0.0,
            }
        )

    series_medias = []
    for coluna, rotulo in rotulos_medias.items():
        if coluna not in medias.columns:
            continue
        pontos = [
            {"time": indice.strftime("%Y-%m-%d"), "value": float(valor)}
            for indice, valor in medias[coluna].dropna().items()
        ]
        if pontos:
            series_medias.append({"rotulo": rotulo, "dados": pontos})

    medias_linhas = []
    for coluna, rotulo in rotulos_medias.items():
        if coluna not in medias.columns or medias[coluna].dropna().empty:
            valor = None
        else:
            valor = float(medias[coluna].dropna().iloc[-1])
        medias_linhas.append(
            {
                "rotulo": rotulo,
                "valor": formato.moeda(valor) if valor is not None else "N/A",
                "motivo": None
                if valor is not None
                else "histórico insuficiente para esta média",
            }
        )

    # Bandas de Bollinger na mesma periodicidade do gráfico (20 períodos, 2σ).
    bandas_df = tecnicos.bollinger(
        trabalho.set_index("data")["fechamento"], janela=20, desvios=2.0
    )
    bandas = {"janela": 20, "desvios": 2.0}
    for coluna in ("superior", "media", "inferior"):
        bandas[coluna] = [
            {"time": indice.strftime("%Y-%m-%d"), "value": float(valor)}
            for indice, valor in bandas_df[coluna].dropna().items()
        ]

    grafico = {
        "tipo": "candles",
        "candles": candles,
        "volumes": volumes,
        "medias": series_medias,
        "bandas": bandas,
    }

    template = tecnicos.trend_template(df)
    rotulos_trend = {
        "preco_acima_sma200": "Preço acima da SMA200 diária",
        "sma150_acima_sma200": "SMA150 diária acima da SMA200",
        "sma200_subindo": "SMA200 diária subindo no mês",
        "sma50_acima_sma150_sma200": "SMA50 acima da SMA150 e da SMA200",
        "preco_acima_sma50": "Preço acima da SMA50 diária",
        "proximo_maxima_52s": "A no máximo 25% da máxima de 52 semanas",
        "acima_minima_52s": "A pelo menos 25% acima da mínima de 52 semanas",
    }
    trend = []
    for chave, rotulo in rotulos_trend.items():
        criterio = template.get(chave)
        if not isinstance(criterio, dict):
            continue
        atende = criterio.get("atende")
        if atende is True:
            status = "atende"
        elif atende is False:
            status = "não atende"
        else:
            status = "indisponível"
        valor = criterio.get("valor")
        trend.append(
            {
                "rotulo": rotulo,
                "status": status,
                "valor": formato.numero(valor, casas=2)
                if valor is not None
                else "N/A",
                "motivo": None
                if valor is not None
                else "histórico insuficiente",
                "regra": criterio.get("regra"),
            }
        )

    atendidos_trend = sum(
        1
        for chave in rotulos_trend
        if isinstance(template.get(chave), dict)
        and template[chave].get("atende") is True
    )
    trend_quase_todos = (
        template.get("atende_todos") is False and atendidos_trend >= 6
    )
    trend_motivo_falta = None
    if trend_quase_todos:
        faltantes = [
            rotulo
            for chave, rotulo in rotulos_trend.items()
            if isinstance(template.get(chave), dict)
            and template[chave].get("atende") is False
        ]
        trend_motivo_falta = (
            "Critério não atendido: " + ", ".join(faltantes)
            if faltantes
            else None
        )

    try:
        benchmark = _cotacoes_df("^BVSP")
        if benchmark.empty:
            raise ValueError("sem cotações do benchmark ^BVSP")
        relativa = tecnicos.forca_relativa(df, benchmark)
        erro_relativa = None
    except ValueError as exc:
        relativa = {}
        erro_relativa = str(exc)
    rotulos_rs = {"1m": "1 mês", "3m": "3 meses", "6m": "6 meses", "12m": "12 meses"}
    forca_relativa = []
    for chave, rotulo in rotulos_rs.items():
        forca_relativa.append(
            {
                "rotulo": rotulo,
                "ativo": formato.percentual(relativa.get(f"ativo_{chave}"), sinal=True),
                "benchmark": formato.percentual(
                    relativa.get(f"benchmark_{chave}"), sinal=True
                ),
                "rs": formato.percentual(relativa.get(f"rs_{chave}"), sinal=True),
            }
        )

    sinais = []
    for sinal in tecnicos.sinais(df):
        atende = sinal.get("atende")
        if atende is True:
            status = "atende"
        elif atende is False:
            status = "não atende"
        else:
            status = "indisponível"
        sinais.append(
            {
                "nome": sinal.get("sinal", "").replace("_", " ").capitalize(),
                "status": status,
                "motivo": sinal.get("motivo"),
            }
        )

    volumes_validos = pd.to_numeric(df["volume"], errors="coerce").dropna()
    volume_ultimo = (
        float(volumes_validos.iloc[-1]) if len(volumes_validos) else None
    )
    volume_medio = (
        float(volumes_validos.iloc[-50:].mean()) if len(volumes_validos) else None
    )

    return {
        "periodo": periodo,
        "sem_dados": False,
        "grafico": grafico,
        "medias": medias_linhas,
        "trend": trend,
        "trend_quase_todos": trend_quase_todos,
        "trend_motivo_falta": trend_motivo_falta,
        "forca_relativa": forca_relativa,
        "erro_relativa": erro_relativa,
        "sinais": sinais,
        "volume_ultimo": formato.inteiro(volume_ultimo),
        "volume_medio": formato.inteiro(volume_medio),
        "data_ref": str(df.iloc[-1]["data"].date()),
    }


def _float(valor: Any) -> float | None:
    convertido = formato.converter_numero(valor)
    return None if convertido is None else float(convertido)


def _contexto_comparacao(
    dados_a: dict[str, Any], dados_b: dict[str, Any]
) -> dict[str, Any]:
    def serie(dados: dict[str, Any]) -> pd.Series:
        registros = {
            pd.Timestamp(item["data"]): item["fechamento"]
            for item in dados["cotacoes"]
            if item.get("fechamento") is not None
        }
        return pd.Series(registros).sort_index()

    a = serie(dados_a).rename("a")
    b = serie(dados_b).rename("b")
    combinado = pd.concat([a, b], axis=1, join="inner").dropna()
    if combinado.empty:
        return {"tipo": "comparacao", "series": []}
    normalizado = combinado.div(combinado.iloc[0]) * 100
    series = []
    for coluna, rotulo in (("a", dados_a["ativo"]["ticker"]), ("b", dados_b["ativo"]["ticker"])):
        dados_serie = [
            {"time": indice.strftime("%Y-%m-%d"), "value": float(valor)}
            for indice, valor in normalizado[coluna].items()
        ]
        series.append({"rotulo": rotulo, "dados": dados_serie})
    return {"tipo": "comparacao", "series": series}


def _contexto_consenso(
    detalhes: dict[str, dict[str, Any]], preco: Any
) -> dict[str, Any]:
    """Resumo do consenso do MSN para o bloco da página do ativo."""

    valores: dict[str, Any] = {}
    datas: list[str] = []
    for chave in consultas.CAMPOS_CONSENSO:
        info = detalhes.get(chave)
        if not info or str(info.get("fonte") or "") != "msn":
            continue
        valores[chave] = info.get("valor")
        data_ref = str(info.get("data_ref") or "")[:10]
        if data_ref:
            datas.append(data_ref)

    if not valores:
        return {"disponivel": False, "motivo": "sem consenso na fonte"}

    def numero(chave: str) -> float | None:
        return formato.converter_numero(valores.get(chave))

    compra = numero("consenso_compra")
    neutro = numero("consenso_neutro")
    venda = numero("consenso_venda")
    total = numero("consenso_total")
    if total is None and None not in (compra, neutro, venda):
        total = compra + neutro + venda

    alvo_medio = numero("alvo_medio")
    alvo_mediana = numero("alvo_mediana")
    alvo_max = numero("alvo_max")
    alvo_min = numero("alvo_min")
    alvo_duvidoso = msn.alvo_classe_duvidosa(alvo_medio, preco)
    upside = msn.upside_pct(alvo_medio, preco)

    data_ref = max(datas) if datas else None
    resumo = consultas.consenso_resumo(
        {**valores, "preco": preco, "consenso_data_ref": data_ref}
    )
    avisos: list[str] = []
    if total is not None and total < 2:
        quantidade = int(total)
        rotulo = "1 analista" if quantidade == 1 else f"{quantidade} analistas"
        avisos.append(f"consenso com {rotulo}: não é consenso")
    if data_ref:
        try:
            idade = (date.today() - date.fromisoformat(data_ref)).days
        except ValueError:
            idade = 0
        if idade > msn.VALIDADE_DIAS:
            avisos.append(f"consenso desatualizado desde {data_ref}")

    def texto(valor: Any, tipo: str, casas: int = 2) -> str:
        if tipo == "moeda":
            return formato.moeda(valor, casas=casas) or "N/A"
        if tipo == "percentual":
            return formato.percentual(valor, casas=casas, sinal=True) or "N/A"
        return formato.inteiro(valor) or "N/A"

    faixa = "N/A"
    if alvo_min is not None or alvo_max is not None:
        faixa = (
            f"{formato.moeda(alvo_min) or 'N/A'} – "
            f"{formato.moeda(alvo_max) or 'N/A'}"
        )

    def percentual_linha(valor: float | None) -> str:
        if valor is None or not total:
            return "N/A"
        return formato.percentual(valor / total * 100, casas=0) or "N/A"

    return {
        "disponivel": True,
        "total": total,
        "total_texto": texto(total, "inteiro"),
        "compra_texto": texto(compra, "inteiro"),
        "neutro_texto": texto(neutro, "inteiro"),
        "venda_texto": texto(venda, "inteiro"),
        "compra_pct_texto": percentual_linha(compra),
        "neutro_pct_texto": percentual_linha(neutro),
        "venda_pct_texto": percentual_linha(venda),
        "recomendacao": valores.get("recomendacao"),
        "alvo_medio_texto": (
            f"{texto(alvo_medio, 'moeda')} · classe em dúvida "
            "(o alvo pode ser da unit/outra classe do papel)"
            if alvo_duvidoso
            else texto(alvo_medio, "moeda")
        ),
        "alvo_mediana_texto": texto(alvo_mediana, "moeda"),
        "faixa": faixa,
        "alvo_classe_duvidosa": alvo_duvidoso,
        "upside_texto": (
            "N/A — classe em dúvida"
            if alvo_duvidoso
            else texto(upside, "percentual")
        ),
        "upside_positivo": upside is None or upside >= 0,
        "data_ref": (resumo or {}).get("data") or formato.data_br(data_ref),
        "avisos": avisos,
    }


def _contexto_estimativas(dados: dict[str, Any]) -> dict[str, Any] | None:
    """Bazin, Graham e Genial para a página do ativo (só ação e BDR)."""

    classe = dados["ativo"].get("classe")
    if classe not in {"acao_b3", "bdr"}:
        return None

    campos = dados["fundamentos"].get("campos") or {}
    detalhes = dados["fundamentos"].get("detalhes") or {}
    preco = dados["ultima"]["fechamento"] if dados["ultima"] else None
    lpa = formato.converter_numero(campos.get("lpa"))
    vpa = formato.converter_numero(campos.get("vpa_calculado"))
    if vpa is None:
        vpa = formato.converter_numero(campos.get("vpa_yahoo"))
    dpa = dados.get("proventos_12m")

    def linha(rotulo: str, valor: Any, detalhe: str, motivo: str) -> dict[str, Any]:
        if valor is None:
            return {
                "rotulo": rotulo,
                "valor_texto": "N/A",
                "margem_texto": "N/A",
                "margem_positiva": None,
                "detalhe": detalhe,
                "motivo": motivo,
            }
        margem = valuation.margem(valor, preco)
        return {
            "rotulo": rotulo,
            "valor_texto": formato.moeda(valor) or "N/A",
            "margem_texto": formato.percentual(margem, sinal=True) or "N/A",
            "margem_positiva": None if margem is None else margem >= 0,
            "detalhe": detalhe,
            "motivo": "",
        }

    bazin_valor = valuation.bazin(dpa)
    bazin_detalhe = (
        f"{formato.moeda(dpa)} de proventos em 12 meses ÷ 6% ao ano"
        if bazin_valor is not None
        else "proventos dos últimos 12 meses"
    )
    bazin_motivo = (
        "sem proventos nos últimos 12 meses"
        if dpa is None or float(dpa) <= 0
        else "sem dado de proventos"
    )

    graham_valor = valuation.graham(lpa, vpa)
    graham_detalhe = (
        f"√(22,5 × LPA {formato.moeda(lpa)} × VPA {formato.moeda(vpa)})"
        if graham_valor is not None
        else "LPA e VPA positivos na fonte"
    )
    if lpa is None or vpa is None:
        graham_motivo = "sem LPA ou VPA na fonte"
    elif lpa <= 0:
        graham_motivo = "LPA negativo: Graham não se aplica"
    elif vpa <= 0:
        graham_motivo = "VPA negativo: Graham não se aplica"
    else:
        graham_motivo = "sem dado para o cálculo"

    linhas = [
        linha("Bazin (DY 6%)", bazin_valor, bazin_detalhe, bazin_motivo),
        linha("Graham", graham_valor, graham_detalhe, graham_motivo),
    ]

    genial_valor = None
    genial_detalhe = "preço-alvo de 12 meses publicado no Genial Analisa"
    genial_motivo = "sem cobertura na Genial (ou coleta não executada)"
    info_genial = detalhes.get("genial_alvo")
    if info_genial and str(info_genial.get("fonte") or "") == "genial":
        genial_valor = formato.converter_numero(info_genial.get("valor"))
        data_alvo = str(info_genial.get("data_ref") or "")[:10]
        partes = []
        recomendacao = (campos.get("genial_recomendacao") or "").strip()
        if recomendacao:
            partes.append(f"recomendação {recomendacao}")
        preco_genial = formato.converter_numero(campos.get("genial_preco"))
        if preco_genial is not None:
            data_preco = formato.data_br(campos.get("genial_preco_data"))
            partes.append(
                f"preço de referência {formato.moeda(preco_genial)}"
                + (f" em {data_preco}" if data_preco else "")
            )
        partes.append(f"consultado em {formato.data_br(data_alvo) or 'N/A'}")
        genial_detalhe = " · ".join(partes)
        genial_motivo = ""
    linhas.append(
        linha("Genial (alvo 12m)", genial_valor, genial_detalhe, genial_motivo)
    )

    btg_valor = None
    btg_detalhe = "preço-alvo de 12 meses publicado no Research BTG"
    btg_motivo = "sem cobertura no BTG (ou coleta não executada)"
    info_btg = detalhes.get("btg_alvo")
    if info_btg and str(info_btg.get("fonte") or "") in {"btg", "btg_planilha"}:
        btg_valor = formato.converter_numero(info_btg.get("valor"))
        partes = []
        recomendacao = (campos.get("btg_recomendacao") or "").strip()
        if recomendacao:
            partes.append(f"recomendação {recomendacao}")
        data_alvo = formato.data_br(campos.get("btg_data"))
        if data_alvo:
            partes.append(f"alvo definido em {data_alvo}")
        consultado = formato.data_br(str(info_btg.get("data_ref") or "")[:10])
        if consultado:
            partes.append(f"consultado em {consultado}")
        btg_detalhe = " · ".join(partes)
        btg_motivo = ""
    linhas.append(linha("BTG (alvo 12m)", btg_valor, btg_detalhe, btg_motivo))

    return {
        "linhas": linhas,
        "preco_texto": formato.moeda(preco) or "N/A",
        "tem_preco": preco is not None,
    }


@app.get("/ativos/{ticker}", response_class=HTMLResponse)
def pagina_ativo(request: Request, ticker: str) -> HTMLResponse:
    dados = consultas.carregar_ativo(ticker)
    if dados is None:
        raise HTTPException(
            status_code=404, detail=f"ativo {ticker.strip().upper()} não encontrado"
        )

    aba = request.query_params.get("aba") or "resumo"
    if aba not in {"resumo", "lista", "tecnico", "historico", "comparar"}:
        aba = "resumo"
    periodo = request.query_params.get("periodo") or "semanal"
    comparar = (request.query_params.get("comparar") or "").strip().upper()
    try:
        pagina = max(1, int(request.query_params.get("pagina") or 1))
    except ValueError:
        pagina = 1

    indicadores = dados["indicadores"]
    data_cotacao = dados["ultima"]["data"] if dados["ultima"] else None
    data_fundamentos = dados["fundamentos"]["data_ref"]
    data_provento = dados["proventos"][0]["data_ex"] if dados["proventos"] else None
    detalhes_fundamentos = dados["fundamentos"].get("detalhes") or {}

    linhas_demonstracoes = [
        _linha_fundamento(
            chave,
            indicadores.get(chave),
            rotulo,
            tipo,
            MOTIVOS_COLUNAS.get(chave),
            detalhes_fundamentos,
            formato.data_br(data_fundamentos),
            indicadores,
        )
        for chave, rotulo, tipo in INDICADORES_DEMONSTRACOES
    ]
    # ETF: patrimônio, taxa de administração e cotistas vêm do Status Invest.
    if dados["ativo"]["classe"] == "etf":
        campos_etf = dados["fundamentos"].get("campos") or {}
        for chave, rotulo, tipo in (
            ("patrimonio_liquido", "Patrimônio líquido", "moeda"),
            ("taxa_administracao_etf", "Taxa de administração", "percentual"),
            ("cotistas", "Cotistas", "inteiro"),
        ):
            if campos_etf.get(chave) is None:
                continue
            linhas_demonstracoes.append(
                _linha_fundamento(
                    chave,
                    campos_etf.get(chave),
                    rotulo,
                    tipo,
                    None,
                    detalhes_fundamentos,
                    formato.data_br(data_fundamentos),
                    indicadores,
                )
            )

    # FII de tijolo: vacância e nº de imóveis vêm do Funds Explorer; só
    # aparecem quando o fundo publica (papel e fundo de fundos não têm).
    if dados["ativo"]["classe"] == "fii":
        campos_fii = dados["fundamentos"].get("campos") or {}
        if campos_fii.get("vacancia") is not None:
            referencia = formato.data_br(campos_fii.get("vacancia_data_ref"))
            linhas_demonstracoes.append(
                _linha_fundamento(
                    "vacancia",
                    campos_fii.get("vacancia"),
                    f"Vacância física ({referencia})" if referencia else "Vacância física",
                    "percentual",
                    None,
                    detalhes_fundamentos,
                    formato.data_br(data_fundamentos),
                    indicadores,
                )
            )
        if campos_fii.get("imoveis") is not None:
            linhas_demonstracoes.append(
                _linha_fundamento(
                    "imoveis",
                    campos_fii.get("imoveis"),
                    "Imóveis",
                    "inteiro",
                    None,
                    detalhes_fundamentos,
                    formato.data_br(data_fundamentos),
                    indicadores,
                )
            )

    linhas_proventos = [
        {
            **_linha_indicador(
                dados["proventos_12m"] if chave == "proventos_12m" else indicadores.get(chave),
                rotulo,
                tipo,
                MOTIVOS_COLUNAS.get(chave, "sem provento no período"),
            ),
            "fonte": (
                "yahoo"
                if chave == "proventos_12m" and dados["proventos"]
                else "calculado"
            ),
            "data_ref": formato.data_br(data_provento),
            "destaque": chave == "dy_12m_calculado",
        }
        for chave, rotulo, tipo in INDICADORES_PROVENTOS
    ]

    tecnico_contexto: dict[str, Any] | None = None
    comparacao = None
    if aba == "tecnico":
        tecnico_contexto = _contexto_tecnico(dados, periodo)
    elif aba == "comparar" and comparar:
        outro = consultas.carregar_ativo(comparar)
        if outro is not None:
            comparacao = _contexto_comparacao(dados, outro)
            comparacao["outro"] = outro["ativo"]["ticker"]
            comparacao["outro_nome"] = outro["ativo"].get("nome")

    historico = None
    if aba == "historico":
        historico = _historico_cotacoes(ticker, pagina, 50)

    consenso = _contexto_consenso(
        detalhes_fundamentos,
        dados["ultima"]["fechamento"] if dados["ultima"] else None,
    )

    return templates.TemplateResponse(
        request=request,
        name="ativo.html",
        context=_contexto(
            request,
            dados=dados,
            ticker=dados["ativo"]["ticker"],
            aba=aba,
            periodo=periodo,
            indicadores=indicadores,
            data_cotacao=formato.data_br(data_cotacao),
            data_fundamentos=formato.data_br(data_fundamentos),
            data_provento=formato.data_br(data_provento),
            linhas_demonstracoes=linhas_demonstracoes,
            linhas_proventos=linhas_proventos,
            consenso=consenso,
            estimativas=_contexto_estimativas(dados),
            links_externos=links.links_externos(
                dados["ativo"]["ticker"],
                dados["ativo"].get("classe"),
                msn.url_do_ativo(dados["ativo"]["ticker"]),
            ),
            por_que=consultas.rankings_do_ticker(ticker),
            tecnico=tecnico_contexto,
            comparar=comparar,
            comparacao=comparacao,
            historico=historico,
        ),
    )


@app.get("/ativos/{ticker}/tecnico", response_class=HTMLResponse)
def pagina_ativo_tecnico(request: Request, ticker: str) -> HTMLResponse:
    dados = consultas.carregar_ativo(ticker)
    if dados is None:
        raise HTTPException(
            status_code=404, detail=f"ativo {ticker.strip().upper()} não encontrado"
        )
    periodo = request.query_params.get("periodo") or "semanal"
    tecnico_contexto = _contexto_tecnico(dados, periodo)
    return templates.TemplateResponse(
        request=request,
        name="ativo.html",
        context=_contexto(
            request,
            dados=dados,
            ticker=dados["ativo"]["ticker"],
            aba="tecnico",
            periodo=periodo if periodo in {"diario", "semanal", "mensal"} else "diario",
            indicadores=dados["indicadores"],
            data_cotacao=formato.data_br(
                dados["ultima"]["data"] if dados["ultima"] else None
            ),
            data_fundamentos=formato.data_br(dados["fundamentos"]["data_ref"]),
            data_provento=formato.data_br(
                dados["proventos"][0]["data_ex"] if dados["proventos"] else None
            ),
            linhas_demonstracoes=[],
            linhas_proventos=[],
            links_externos=links.links_externos(
                dados["ativo"]["ticker"],
                dados["ativo"].get("classe"),
                msn.url_do_ativo(dados["ativo"]["ticker"]),
            ),
            por_que=consultas.rankings_do_ticker(ticker),
            tecnico=tecnico_contexto,
            comparar=None,
            comparacao=None,
            historico=None,
        ),
    )


def _historico_cotacoes(
    ticker: str, pagina: int, por_pagina: int = 50
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    with db.conectar() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM cotacoes WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
        )
        linhas = conn.execute(
            """
            SELECT data, abertura, maxima, minima, fechamento,
                   fechamento_ajustado, volume, fonte
              FROM cotacoes
             WHERE ticker = ?
             ORDER BY data DESC
             LIMIT ? OFFSET ?
            """,
            (ticker, por_pagina, (pagina - 1) * por_pagina),
        ).fetchall()
    paginas = max(1, (total + por_pagina - 1) // por_pagina)
    return {
        "linhas": [_linha_para_dict(linha) for linha in linhas],
        "total": total,
        "pagina": pagina,
        "paginas": paginas,
        "por_pagina": por_pagina,
    }


COMPARAR_INDICADORES: list[tuple[str, str, str, str]] = [
    ("preco", "Preço", "moeda", "menor"),
    ("pl", "P/L", "numero", "menor"),
    ("pvp", "P/VP", "numero", "menor"),
    ("dy", "Dividend yield", "percentual", "maior"),
    ("roe", "ROE", "percentual", "maior"),
    ("roic", "ROIC", "percentual", "maior"),
    ("margem_liquida", "Margem líquida", "percentual", "maior"),
    ("divida_liquida_ebitda", "Dívida líquida/EBITDA", "numero", "menor"),
    ("ev_ebitda", "EV/EBITDA", "numero", "menor"),
    ("payout", "Payout", "percentual", "menor"),
    ("crescimento_receita_5a", "Crescimento de receita 5a", "percentual", "maior"),
]


@app.get("/comparar", response_class=HTMLResponse)
def pagina_comparar(request: Request) -> HTMLResponse:
    bruto = request.query_params.get("tickers") or ""
    tickers = [parte.strip().upper() for parte in bruto.split(",") if parte.strip()]
    tickers = list(dict.fromkeys(tickers))[:8]
    ativos = []
    ausentes = []
    for ticker in tickers:
        dados = consultas.carregar_ativo(ticker)
        if dados is None:
            ausentes.append(ticker)
        else:
            ativos.append(dados)

    linhas = []
    for chave, rotulo, tipo, sentido in COMPARAR_INDICADORES:
        valores = []
        for dados in ativos:
            if chave == "proventos_12m":
                valor = dados.get(chave)
            else:
                valor = dados["indicadores"].get(chave)
            if chave == "dy" and valor is None:
                valor = dados["indicadores"].get("dy_12m_calculado")
            valores.append(valor)
        numeros = [formato.converter_numero(valor) for valor in valores]
        presentes = [numero for numero in numeros if numero is not None]
        melhor = None
        if presentes:
            melhor = min(presentes) if sentido == "menor" else max(presentes)
        celulas = []
        for valor, numero in zip(valores, numeros):
            celula = formato.celula(
                valor, tipo=tipo, motivo=MOTIVOS_COLUNAS.get(chave)
            )
            celula["melhor"] = (
                numero is not None and melhor is not None and numero == melhor
            )
            celulas.append(celula)
        linhas.append(
            {"chave": chave, "rotulo": rotulo, "sentido": sentido, "celulas": celulas}
        )

    return templates.TemplateResponse(
        request=request,
        name="comparar.html",
        context=_contexto(
            request,
            tickers=tickers,
            bruto=bruto,
            ativos=ativos,
            ausentes=ausentes,
            linhas=linhas,
        ),
    )


def _achatar_config(valor: Any, prefixo: str = "") -> list[dict[str, str]]:
    linhas: list[dict[str, str]] = []
    if isinstance(valor, dict):
        for chave, conteudo in valor.items():
            rotulo = f"{prefixo}.{chave}" if prefixo else str(chave)
            linhas.extend(_achatar_config(conteudo, rotulo))
    elif isinstance(valor, (list, tuple)):
        linhas.append(
            {
                "chave": prefixo,
                "valor": ", ".join(str(item) for item in valor),
            }
        )
    else:
        linhas.append({"chave": prefixo, "valor": str(valor)})
    return linhas


DESCRICOES_ESTRATEGIAS: dict[str, str] = {
    "magic_formula": (
        "EBIT/EV e retorno sobre o capital investido (ROC), soma de postos; "
        "EV/EBIT é o inverso de EBIT/EV e não é uma estratégia separada"
    ),
    "upside_consenso": (
        "maior upside até o preço-alvo médio do consenso MSN, com cobertura "
        "mínima de analistas e alvo da mesma classe do papel"
    ),
}


@app.get("/estrategias", response_class=HTMLResponse)
def pagina_estrategias(request: Request) -> HTMLResponse:
    secoes = []
    for nome, configuracao in estrategias.CONFIG_PADRAO.items():
        secoes.append(
            {
                "chave": nome,
                "nome": consultas.nome_estrategia(nome)
                if nome in consultas.NOMES_ESTRATEGIAS
                else nome.replace("_", " ").title(),
                "descricao": DESCRICOES_ESTRATEGIAS.get(nome),
                "linhas": _achatar_config(configuracao),
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="estrategias.html",
        context=_contexto(request, secoes=secoes),
    )


@app.get("/fontes", response_class=HTMLResponse)
def pagina_fontes(request: Request) -> HTMLResponse:
    coleta = consultas.ultima_coleta()
    auto_refresh = None
    resumo_coleta = None
    if coleta is not None:
        if coleta.get("status") == "em_andamento":
            auto_refresh = 15
        else:
            try:
                resumo_coleta = json.loads(coleta.get("resumo_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                resumo_coleta = {}
            if not isinstance(resumo_coleta, dict):
                resumo_coleta = {}
    proxima_coleta = next(
        (
            item["proxima_execucao"]
            for item in agenda.status()
            if item["job"] == "cotacoes"
        ),
        None,
    )
    return templates.TemplateResponse(
        request=request,
        name="fontes.html",
        context=_contexto(
            request,
            fontes=consultas.resumo_fontes(),
            mapeamento=consultas.mapeamento_fii(),
            coleta=coleta,
            resumo_coleta=resumo_coleta,
            auto_refresh=auto_refresh,
            agendamento=agenda.agendamento(),
            proxima_coleta=proxima_coleta,
            aviso=request.query_params.get("aviso"),
        ),
    )


@app.post("/fontes/agenda")
async def salvar_agendamento(request: Request) -> RedirectResponse:
    form = await request.form()
    frequencia = str(form.get("frequencia") or "").strip()
    horario = str(form.get("horario") or "").strip()
    try:
        frequencia, horario = agenda.validar_agendamento(frequencia, horario)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/fontes?{urlencode({'aviso': str(exc)})}", status_code=303
        )
    db.gravar_configuracao("agenda_frequencia", frequencia)
    db.gravar_configuracao("agenda_horario", horario)
    agenda.aplicar_agendamento()
    aviso = f"agendamento salvo: {frequencia} às {horario}"
    return RedirectResponse(
        url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
    )


@app.post("/fontes/planilha-btg")
async def enviar_planilha_btg(arquivo: UploadFile = File(...)) -> RedirectResponse:
    """Recebe o Stock Guide do BTG em Excel e grava os alvos."""

    nome = (arquivo.filename or "").strip()
    if not nome.lower().endswith((".xlsx", ".xlsm")):
        aviso = "envie a planilha em .xlsx (o .xls antigo não é lido)"
    else:
        conteudo = await arquivo.read()
        if not conteudo:
            aviso = "arquivo vazio"
        elif len(conteudo) > 8 * 1024 * 1024:
            aviso = "planilha acima de 8 MB recusada"
        else:
            from app.fontes import btg_planilha

            resumo = btg_planilha.importar(conteudo)
            if resumo.get("erro"):
                aviso = f"planilha BTG: {resumo['erro']}"
            else:
                aviso = (
                    f"planilha BTG: {resumo['gravados']} ticker(s) gravado(s) "
                    f"de {resumo['lidos']} lido(s) "
                    f"({resumo['campos']} campo(s))"
                )
                if resumo.get("avisos"):
                    aviso += " · " + " · ".join(resumo["avisos"])
    return RedirectResponse(
        url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
    )


@app.get("/alertas", response_class=HTMLResponse)
def pagina_alertas(request: Request) -> HTMLResponse:
    parametro = request.query_params.get("lidos")
    lidos = int(parametro) if parametro in {"0", "1"} else None
    itens = alertas.listar(lidos=lidos)
    return templates.TemplateResponse(
        request=request,
        name="alertas.html",
        context=_contexto(
            request,
            alertas=itens,
            tipos=alertas.TIPOS,
            lidos=lidos,
            nao_lidos=alertas.contar_nao_lidos(),
        ),
    )


@app.post("/alertas/{id}/lido")
def marcar_alerta_lido(id: int) -> RedirectResponse:
    alertas.marcar_lido(id)
    return RedirectResponse(url="/alertas", status_code=303)


@app.post("/atualizar")
async def atualizar_form(request: Request, background: BackgroundTasks) -> RedirectResponse:
    form = await request.form()
    tickers = str(form.get("tickers") or "").strip()
    periodo = str(form.get("periodo") or config.PERIODO_PADRAO).strip()
    if tickers:
        lista = [item.strip().upper() for item in tickers.split(",") if item.strip()]
    else:
        try:
            lista = list(config.ler_universo().keys())
        except FileNotFoundError:
            lista = []
    if not lista:
        aviso = "nenhum ticker para atualizar"
        return RedirectResponse(
            url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
        )
    id_coleta = db.abrir_coleta_ou_recusar("manual")
    if id_coleta is None:
        aviso = "coleta já em andamento"
        return RedirectResponse(
            url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
        )
    fonte = str(form.get("fonte") or "").strip()
    if fonte in {"estimativas", "consenso"}:
        background.add_task(_executar_coleta_fonte, id_coleta, fonte, lista)
        aviso = f"coleta de {fonte} iniciada (id {id_coleta})"
        return RedirectResponse(
            url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
        )
    background.add_task(_executar_coleta, id_coleta, lista, periodo)
    aviso = f"coleta iniciada (id {id_coleta})"
    return RedirectResponse(
        url=f"/fontes?{urlencode({'aviso': aviso})}", status_code=303
    )


def _executar_coleta_fonte(id_coleta: int, fonte: str, tickers: list[str]) -> None:
    """Coleta avulsa de uma fonte de estimativa, disparada pela tela."""

    try:
        from scripts import coletar as coletor

        if fonte == "estimativas":
            resumo = {
                "btg": coletor.coletar_btg(tickers),
                "genial": coletor.coletar_genial(tickers),
            }
            falhas = sum(int(item.get("falhas", 0)) for item in resumo.values())
            status = "ok" if falhas == 0 else "parcial"
        else:
            resumo = coletor.coletar_consenso(tickers)
            status = "ok" if resumo.get("falhas", 0) == 0 else "parcial"
        db.finalizar_coleta(id_coleta, status, resumo)
    except Exception as exc:  # noqa: BLE001
        logger.exception("falha na coleta de %s (%s)", fonte, id_coleta)
        db.registrar_falha("sistema", None, fonte, f"{type(exc).__name__}: {exc}")
        db.finalizar_coleta(id_coleta, "falha", {"erro": str(exc)})


def _executar_coleta(id_coleta: int, tickers: list[str], periodo: str) -> None:
    """Coleta disparada pela tela; refaz as listas com o dado novo.

    O job agendado já encadeia rankings e alertas; o botão precisa fazer o
    mesmo, senão a coleta grava fundamentos e as listas continuam velhas.
    """

    try:
        from scripts.coletar import coletar

        resumo = coletar(tickers, periodo)
        status = "ok" if resumo.get("falhas", 0) == 0 else "parcial"
        db.finalizar_coleta(id_coleta, status, resumo)
    except Exception as exc:  # noqa: BLE001
        logger.exception("falha na coleta %s", id_coleta)
        db.registrar_falha("sistema", None, "coleta", f"{type(exc).__name__}: {exc}")
        db.finalizar_coleta(id_coleta, "falha", {"erro": str(exc)})
        return

    try:
        from app.agenda import _job_rankings

        _job_rankings()
    except Exception as exc:  # noqa: BLE001 - a coleta já foi gravada
        logger.exception("falha ao refazer rankings da coleta %s", id_coleta)
        db.registrar_falha(
            "sistema", None, "rankings", f"{type(exc).__name__}: {exc}"
        )


@app.post("/api/atualizar")
def atualizar(
    background: BackgroundTasks,
    tickers: str | None = Query(default=None),
    periodo: str = Query(default=config.PERIODO_PADRAO),
) -> dict[str, Any]:
    if tickers:
        lista = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    else:
        lista = list(config.ler_universo().keys())

    if not lista:
        raise HTTPException(status_code=400, detail="nenhum ticker para atualizar")

    id_coleta = db.abrir_coleta_ou_recusar("manual")
    if id_coleta is None:
        raise HTTPException(status_code=409, detail="coleta já em andamento")
    background.add_task(_executar_coleta, id_coleta, lista, periodo)
    return {"coleta": id_coleta}
