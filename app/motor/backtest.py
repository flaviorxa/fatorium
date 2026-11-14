"""Backtest com corte temporal rígido.

O motor usa somente o que existia na data de decisão: cotações com
``data <= d`` e a foto de fundamentos mais recente com ``data_ref <= d``. Não há
ajuste "com o benefício da dúvida": nenhum dado posterior à decisão entra no
cálculo.

Limitações estruturais desta base (também devolvidas em ``limitacoes``):

* a tabela ``fundamentos`` guarda apenas as fotos que a coleta tirou, com
  ``data_ref`` igual à data da coleta. Não existe demonstração histórica
  ponto-no-tempo nas fontes usadas; uma estratégia fundamentalista só é
  honesta da primeira foto em diante;
* ativos deslistados só entram se ainda estiverem no banco;
* proventos são uma aproximação: entram como retorno, sem reinvestimento
  fracionário nem tratamento fiscal.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from app import config as app_config
from app import db
from app.motor import estrategias, tecnicos
from app.motor.indicadores import avaliar_fundamentalista
from app.motor.rankings import classes_da_estrategia

ESTRATEGIAS_FUNDAMENTALISTAS = {
    "magic_formula",
    "graham",
    "bazin",
    "dividendos",
    "qualidade",
    "qualidade_crescimento",
}
ESTRATEGIAS_PRECO = {"trend_template", "forca_relativa"}
ALIASES = {
    "qualidade_crescimento": "qualidade",
    "trend": "trend_template",
    "forca-relativa": "forca_relativa",
}
BENCHMARK_PADRAO = "^BVSP"

CAMPOS_FOTO_COMPLETA = (
    "ebit_12m",
    "preco",
    "nro_acoes",
    "divida_bruta",
    "caixa",
    "patrimonio_liquido",
)
MINIMO_ATIVOS_FOTO_COMPLETA = 20

CAMPOS_ESSENCIAIS: dict[str, tuple[str, ...]] = {
    "magic_formula": ("ebit_12m", "valor_firma", "capital_investido"),
    "graham": ("pl", "pvp"),
    "bazin": (),
    "dividendos": ("dy", "payout", "divida_liquida_ebitda", "roe"),
    "qualidade": ("roic", "roe", "margem_liquida"),
}


def _data(valor: Any) -> date:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def _num(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if math.isnan(numero) or math.isinf(numero):
        return None
    return numero


def _normalizar_estrategia(nome: str) -> str:
    chave = str(nome or "").strip().lower()
    return ALIASES.get(chave, chave)


def _drawdown_maximo(valores: list[float]) -> float:
    """Maior queda percentual do pico até o fundo (valor positivo)."""

    pico: float | None = None
    maior = 0.0
    for valor in valores:
        if valor is None:
            continue
        if pico is None or valor > pico:
            pico = valor
        if pico and pico > 0 and valor < pico:
            maior = max(maior, (pico - valor) / pico * 100)
    return float(maior)


def _carregar_ativos(conn: Any) -> dict[str, dict[str, Any]]:
    linhas = conn.execute(
        "SELECT ticker, nome, classe, setor, moeda FROM ativos ORDER BY ticker"
    ).fetchall()
    return {str(linha["ticker"]).upper(): dict(linha) for linha in linhas}


def _carregar_cotacoes(conn: Any, fim: date) -> dict[str, pd.DataFrame]:
    linhas = conn.execute(
        """
        SELECT ticker, data, abertura, maxima, minima, fechamento, volume, fonte
          FROM cotacoes
         WHERE data <= ?
         ORDER BY ticker, data
        """,
        (fim.isoformat(),),
    ).fetchall()

    por_ticker: dict[str, list[dict[str, Any]]] = {}
    for linha in linhas:
        por_ticker.setdefault(str(linha["ticker"]).upper(), []).append(
            {
                "data": linha["data"],
                "abertura": linha["abertura"],
                "maxima": linha["maxima"],
                "minima": linha["minima"],
                "fechamento": linha["fechamento"],
                "volume": linha["volume"],
                "fonte": linha["fonte"],
            }
        )

    resultado: dict[str, pd.DataFrame] = {}
    for ticker, registros in por_ticker.items():
        df = pd.DataFrame(registros)
        df["data"] = pd.to_datetime(df["data"], errors="coerce")
        df = df.dropna(subset=["data"]).set_index("data").sort_index()
        resultado[ticker] = df
    return resultado


def _carregar_fundamentos(conn: Any, fim: date) -> tuple[dict[str, dict[date, dict[str, Any]]], date | None]:
    linhas = conn.execute(
        """
        SELECT ticker, data_ref, campo, valor
          FROM fundamentos
         WHERE data_ref <= ?
         ORDER BY ticker, data_ref, rowid
        """,
        (fim.isoformat(),),
    ).fetchall()

    por_ticker: dict[str, dict[date, dict[str, Any]]] = {}
    for linha in linhas:
        try:
            referencia = date.fromisoformat(str(linha["data_ref"])[:10])
        except (TypeError, ValueError):
            continue
        ticker = str(linha["ticker"]).upper()
        por_ticker.setdefault(ticker, {}).setdefault(referencia, {})[
            str(linha["campo"])
        ] = linha["valor"]

    primeira = conn.execute("SELECT MIN(data_ref) FROM fundamentos").fetchone()[0]
    primeira_foto = None
    if primeira:
        try:
            primeira_foto = date.fromisoformat(str(primeira)[:10])
        except (TypeError, ValueError):
            primeira_foto = None
    return por_ticker, primeira_foto


def _primeira_foto_completa(
    conn: Any,
    minimo: int = MINIMO_ATIVOS_FOTO_COMPLETA,
) -> date | None:
    """Primeira ``data_ref`` com fundamentos essenciais para ``minimo`` ativos.

    Uma foto completa exige, no mesmo ``data_ref``, os campos que sustentam a
    Magic Formula: ``ebit_12m``, ``patrimonio_liquido`` e os termos capazes de
    derivar ``valor_firma`` (``preco``, ``nro_acoes``, ``divida_bruta`` e
    ``caixa``). Sem pelo menos ``minimo`` ativos nessa condição, o backtest
    fundamentalista não tem base honesta para começar. A varredura é de todo o
    banco, para a cobertura não depender do ``fim`` pedido.
    """

    marcadores = ", ".join("?" for _ in CAMPOS_FOTO_COMPLETA)
    linhas = conn.execute(
        f"SELECT ticker, data_ref, campo, valor FROM fundamentos "
        f"WHERE campo IN ({marcadores})",
        CAMPOS_FOTO_COMPLETA,
    ).fetchall()

    primeira_cotacao: dict[str, date] = {}
    for linha in conn.execute(
        "SELECT ticker, MIN(data) AS primeira FROM cotacoes GROUP BY ticker"
    ):
        try:
            primeira_cotacao[str(linha["ticker"]).upper()] = date.fromisoformat(
                str(linha["primeira"])[:10]
            )
        except (TypeError, ValueError):
            continue

    completos: dict[date, dict[str, set[str]]] = {}
    for linha in linhas:
        if _num(linha["valor"]) is None:
            continue
        try:
            referencia = date.fromisoformat(str(linha["data_ref"])[:10])
        except (TypeError, ValueError):
            continue
        ticker = str(linha["ticker"]).upper()
        completos.setdefault(referencia, {}).setdefault(ticker, set()).add(
            str(linha["campo"])
        )

    todos = set(CAMPOS_FOTO_COMPLETA)
    # ``preco`` pode vir da própria foto (Fundamentus) ou da última cotação
    # disponível até a data de referência, que é como o motor deriva o EV.
    sem_preco = todos - {"preco"}
    for referencia in sorted(completos):
        quantidade = 0
        for ticker, campos in completos[referencia].items():
            if campos == todos:
                quantidade += 1
            elif campos == sem_preco:
                inicio_cotacao = primeira_cotacao.get(ticker)
                if inicio_cotacao is not None and inicio_cotacao <= referencia:
                    quantidade += 1
        if quantidade >= minimo:
            return referencia
    return None


def _carregar_proventos(conn: Any, fim: date) -> dict[str, list[tuple[date, float]]]:
    linhas = conn.execute(
        """
        SELECT ticker, data_ex, valor
          FROM proventos
         WHERE fonte = 'yahoo' AND data_ex <= ?
         ORDER BY ticker, data_ex
        """,
        (fim.isoformat(),),
    ).fetchall()

    resultado: dict[str, list[tuple[date, float]]] = {}
    for linha in linhas:
        valor = _num(linha["valor"])
        if valor is None or valor <= 0:
            continue
        try:
            quando = date.fromisoformat(str(linha["data_ex"])[:10])
        except (TypeError, ValueError):
            continue
        resultado.setdefault(str(linha["ticker"]).upper(), []).append((quando, valor))
    return resultado


def _primeira_cotacao(cotacoes: dict[str, pd.DataFrame], benchmark: str) -> date | None:
    datas = [
        indice.date()
        for ticker, df in cotacoes.items()
        if ticker != benchmark
        for indice in (df.index[:1] if len(df) else [])
    ]
    return min(datas) if datas else None


def _snapshot(
    fundamentos: dict[str, dict[date, dict[str, Any]]], ticker: str, quando: date
) -> tuple[date, dict[str, Any]] | None:
    fotos = fundamentos.get(ticker) or {}
    validas = [referencia for referencia in fotos if referencia <= quando]
    if not validas:
        return None
    referencia = max(validas)
    return referencia, dict(fotos[referencia])


def _regularidade_asof(proventos: list[tuple[date, float]], quando: date) -> float | None:
    limite = quando - timedelta(days=730)
    meses = {
        (quando_ex.year, quando_ex.month)
        for quando_ex, valor in proventos
        if limite <= quando_ex <= quando and valor > 0
    }
    return float(len(meses))


def _preco_em(df: pd.DataFrame | None, quando: Any) -> float | None:
    if df is None or df.empty:
        return None
    recorte = df.loc[df.index <= pd.Timestamp(quando)]
    if recorte.empty:
        return None
    return _num(recorte["fechamento"].iloc[-1])


def _barras_ate(df: pd.DataFrame | None, quando: Any) -> int:
    if df is None or df.empty:
        return 0
    return int((df.index <= pd.Timestamp(quando)).sum())


def _tem_essenciais(estrategia: str, campos: dict[str, Any]) -> bool:
    essenciais = CAMPOS_ESSENCIAIS.get(estrategia, ())
    for campo in essenciais:
        if _num(campos.get(campo)) is not None:
            continue
        alternativas = {
            "ebit_12m": ("ebit",),
            "valor_firma": ("ev", "enterprise_value"),
            "capital_investido": ("patrimonio_liquido", "divida_liquida"),
            "lucro_12m": ("lucro_liquido", "lpa"),
            "divida_liquida_patrim": ("divida_liquida", "patrimonio_liquido"),
        }.get(campo, ())
        if not any(_num(campos.get(alt)) is not None for alt in alternativas):
            return False
    if estrategia == "magic_formula":
        if _num(campos.get("capital_investido")) is None:
            if not (
                _num(campos.get("patrimonio_liquido")) is not None
                and _num(campos.get("divida_liquida")) is not None
            ):
                return False
    return True


def _proventos_12m_asof(
    proventos: list[tuple[date, float]], quando: date
) -> float | None:
    limite = quando - timedelta(days=365)
    total = sum(
        valor
        for quando_ex, valor in proventos
        if limite < quando_ex <= quando and valor > 0
    )
    return float(total) if total > 0 else None


def ativo_as_of(
    ticker: str,
    data: date,
    *,
    exigir_foto: bool = True,
    _ativos: dict[str, dict[str, Any]] | None = None,
    _cotacoes: dict[str, pd.DataFrame] | None = None,
    _fundamentos: dict[str, dict[date, dict[str, Any]]] | None = None,
    _proventos: dict[str, list[tuple[date, float]]] | None = None,
) -> dict[str, Any] | None:
    """Monta o ativo com somente o que existia em ``data``.

    É a mesma montagem usada pela API: dados cadastrais, último preço até
    ``data``, a foto de fundamentos mais recente até ``data`` e os indicadores
    derivados por :func:`avaliar_fundamentalista` (inclusive ``valor_firma``).
    Sem preço, o ativo está fora do universo naquele dia e o retorno é ``None``.
    """

    quando = _data(data)
    ticker = str(ticker).upper()

    if (
        _ativos is None
        or _cotacoes is None
        or _fundamentos is None
        or _proventos is None
    ):
        with db.conectar() as conn:
            if _ativos is None:
                _ativos = _carregar_ativos(conn)
            if _cotacoes is None:
                _cotacoes = _carregar_cotacoes(conn, quando)
            if _fundamentos is None:
                _fundamentos, _ = _carregar_fundamentos(conn, quando)
            if _proventos is None:
                _proventos = _carregar_proventos(conn, quando)

    ativo = dict((_ativos or {}).get(ticker) or {})
    df = (_cotacoes or {}).get(ticker)
    preco = _preco_em(df, quando)
    if preco is None or preco <= 0:
        return None

    snapshot = _snapshot(_fundamentos or {}, ticker, quando)
    if snapshot is None:
        if exigir_foto:
            return None
        referencia: date | None = None
        campos: dict[str, Any] = {}
    else:
        referencia, campos = snapshot
        campos = dict(campos)

    proventos = (_proventos or {}).get(ticker, [])
    indicadores = avaliar_fundamentalista(
        {**campos, "preco": preco},
        ticker=ticker,
        proventos_12m=_proventos_12m_asof(proventos, quando),
        preco_fechamento=preco,
        classe=ativo.get("classe"),
        consultar_proventos=False,
    )

    base: dict[str, Any] = {
        **campos,
        **indicadores,
        "ticker": ticker,
        "nome": ativo.get("nome"),
        "classe": ativo.get("classe"),
        "setor": ativo.get("setor") or campos.get("segmento_cvm"),
        "preco": preco,
        "data_ref": referencia.isoformat() if referencia else None,
        "proventos": [
            {"data_ex": quando_ex.isoformat(), "valor": valor}
            for quando_ex, valor in proventos
            if quando_ex <= quando
        ],
        "proventos_ja_por_acao": True,
    }
    regularidade = _regularidade_asof(proventos, quando)
    if regularidade is not None:
        base["meses_com_provento_24m"] = regularidade
        base["regularidade_meses"] = regularidade
    return base


ROTULOS_ESSENCIAIS = {
    "valor_firma": "valor_firma",
    "ebit_12m": "EBIT",
    "capital_investido": "capital investido",
    "pl": "P/L",
    "pvp": "P/VP",
    "dy": "DY",
    "payout": "payout",
    "divida_liquida_ebitda": "dívida líquida/EBITDA",
    "roe": "ROE",
    "roic": "ROIC",
    "margem_liquida": "margem líquida",
}


def _motivos_essenciais(estrategia: str, campos: dict[str, Any]) -> list[str]:
    """Lista todos os campos essenciais ausentes, para a limitação contar cada um."""

    if _tem_essenciais(estrategia, campos):
        return []
    motivos: list[str] = []
    for campo in CAMPOS_ESSENCIAIS.get(estrategia, ()):
        if _num(campos.get(campo)) is not None:
            continue
        alternativas = {
            "ebit_12m": ("ebit",),
            "valor_firma": ("ev", "enterprise_value"),
            "capital_investido": ("patrimonio_liquido", "divida_liquida"),
            "lucro_12m": ("lucro_liquido", "lpa"),
            "divida_liquida_patrim": ("divida_liquida", "patrimonio_liquido"),
        }.get(campo, ())
        if campo == "capital_investido":
            if all(_num(campos.get(alt)) is not None for alt in alternativas):
                continue
        elif any(_num(campos.get(alt)) is not None for alt in alternativas):
            continue
        motivos.append(f"sem {ROTULOS_ESSENCIAIS.get(campo, campo)}")
    return motivos or ["sem dados essenciais"]


def _selecionar(
    estrategia: str,
    quando: Any,
    tickers: list[str],
    cotacoes: dict[str, pd.DataFrame],
    ativos: dict[str, dict[str, Any]],
    fundamentos: dict[str, dict[date, dict[str, Any]]],
    proventos: dict[str, list[tuple[date, float]]],
    benchmark_series: pd.Series | None,
    config: dict[str, Any],
    top_n: int,
) -> tuple[list[str], dict[str, int]]:
    quando_data = pd.Timestamp(quando).date()
    fundamentalista = estrategia in ESTRATEGIAS_FUNDAMENTALISTAS
    exige_foto = estrategia not in ESTRATEGIAS_PRECO
    permitidas = classes_da_estrategia(estrategia)
    candidatos: list[tuple[Any, ...]] = []
    falhas: dict[str, int] = {}

    def falhar(motivo: str) -> None:
        falhas[motivo] = falhas.get(motivo, 0) + 1

    for ticker in tickers:
        df = cotacoes.get(ticker)
        if estrategia in ESTRATEGIAS_PRECO and _barras_ate(df, quando) < 260:
            falhar("sem histórico suficiente")
            continue
        if _preco_em(df, quando) is None:
            falhar("sem preço")
            continue

        base = ativo_as_of(
            ticker,
            quando_data,
            exigir_foto=exige_foto,
            _ativos=ativos,
            _cotacoes=cotacoes,
            _fundamentos=fundamentos,
            _proventos=proventos,
        )
        if base is None:
            falhar("sem foto de fundamentos")
            continue

        classe = base.get("classe")
        permitida = classe in permitidas
        if (
            not permitida
            and classe == "bdr"
            and fundamentalista
            and estrategias.tem_fundamentos_empresa(base)
        ):
            permitida = True
        if not permitida:
            falhar("classe fora da estratégia")
            continue

        if fundamentalista:
            faltas = _motivos_essenciais(estrategia, base)
            if faltas:
                for falta in faltas:
                    falhar(falta)
                continue
            funcao = estrategias.ESTRATEGIAS.get(estrategia)
            if funcao is None:
                falhar("estratégia sem avaliador")
                continue
            try:
                resultado = funcao(base, config)
            except Exception:  # dado sujo de fonte não derruba a simulação inteira
                falhar("erro ao avaliar fundamentos")
                continue
            if not resultado.get("elegivel"):
                falhar("inelegível pelos critérios")
                continue
            dados = resultado.get("dados") or {}
            pontuacao = resultado.get("pontuacao")
            if estrategia == "magic_formula":
                ey = _num(dados.get("earnings_yield"))
                roc = _num(dados.get("return_on_capital"))
                if ey is None or roc is None:
                    falhar("sem earnings yield/ROC")
                    continue
                candidatos.append(
                    (
                        "magic",
                        ticker,
                        ey,
                        roc,
                        _num(dados.get("return_on_capital")) or 0.0,
                    )
                )
            else:
                if pontuacao is None:
                    falhar("sem pontuação")
                    continue
                desempate = {
                    "graham": _num(dados.get("pl_pvp")),
                    "bazin": _num(dados.get("dividendos_medios_por_acao")),
                    "dividendos": _num(dados.get("dy")),
                    "qualidade": _num(dados.get("roic")),
                }.get(estrategia)
                candidatos.append(("normal", ticker, float(pontuacao), desempate))
            continue

        if estrategia == "trend_template":
            recorte = df.loc[df.index <= pd.Timestamp(quando)]
            try:
                template = tecnicos.trend_template(recorte)
            except Exception:
                falhar("erro no trend template")
                continue
            criterios = {
                chave: valor
                for chave, valor in template.items()
                if isinstance(valor, dict)
            }
            disponiveis = template.get("criterios_disponiveis") or 0
            if not disponiveis:
                falhar("sem histórico suficiente")
                continue
            aprovados = sum(1 for valor in criterios.values() if valor.get("atende") is True)
            pontuacao = aprovados / int(disponiveis) * 100
            distancia = (criterios.get("proximo_maxima_52s") or {}).get("valor")
            atende_todos = template.get("atende_todos") is True
            candidatos.append(
                (
                    "trend",
                    ticker,
                    1 if atende_todos else 0,
                    float(pontuacao),
                    -_num(distancia) if _num(distancia) is not None else 0.0,
                )
            )
            continue

        if estrategia == "forca_relativa":
            if benchmark_series is None or benchmark_series.empty:
                falhar("benchmark indisponível")
                continue
            recorte = df.loc[df.index <= pd.Timestamp(quando)]
            referencia = benchmark_series.loc[
                benchmark_series.index <= pd.Timestamp(quando)
            ]
            try:
                relativa = tecnicos.forca_relativa(recorte, referencia)
            except (ValueError, TypeError):
                falhar("erro na força relativa")
                continue
            for chave in ("rs_12m", "rs_6m", "rs_3m", "rs_1m"):
                valor = _num(relativa.get(chave))
                if valor is not None:
                    candidatos.append(("rs", ticker, valor))
                    break

    if not candidatos:
        return [], falhas

    if estrategia == "magic_formula":
        ordenados_ey = sorted(candidatos, key=lambda item: (-item[2], -item[3]))
        postos: dict[str, dict[str, int]] = {}
        for indice, item in enumerate(ordenados_ey, start=1):
            postos.setdefault(item[1], {})["ey"] = indice
        ordenados_roc = sorted(candidatos, key=lambda item: (-item[3], -item[2]))
        for indice, item in enumerate(ordenados_roc, start=1):
            postos.setdefault(item[1], {})["roc"] = indice
        candidatos.sort(
            key=lambda item: (
                postos[item[1]]["ey"] + postos[item[1]]["roc"],
                -item[3],
                item[1],
            )
        )
        return [item[1] for item in candidatos[:top_n]], falhas

    if estrategia == "trend_template":
        candidatos.sort(key=lambda item: (-item[2], -item[3], item[4], item[1]))
        return [item[1] for item in candidatos[:top_n]], falhas

    if estrategia in {"graham", "bazin", "dividendos", "qualidade"}:
        # Maior pontuação primeiro; o critério de desempate é específico.
        if estrategia == "graham":
            candidatos.sort(
                key=lambda item: (
                    -item[2],
                    item[3] if item[3] is not None else float("inf"),
                    item[1],
                )
            )
        else:
            candidatos.sort(
                key=lambda item: (
                    -item[2],
                    -(item[3] if item[3] is not None else float("-inf")),
                    item[1],
                )
            )
        return [item[1] for item in candidatos[:top_n]], falhas

    candidatos.sort(key=lambda item: (-item[2], item[1]))
    return [item[1] for item in candidatos[:top_n]], falhas


def _calendario(cotacoes: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    datas: set[pd.Timestamp] = set()
    for df in cotacoes.values():
        datas.update(df.index)
    return sorted(datas)


def _datas_decisao(
    inicio: date, fim: date, meses: int, calendario: list[pd.Timestamp]
) -> list[pd.Timestamp]:
    if not calendario:
        return []
    limite = pd.Timestamp(fim)
    candidatas = [indice for indice in calendario if pd.Timestamp(inicio) <= indice <= limite]
    if not candidatas:
        return []
    primeira = candidatas[0]
    decisões = [primeira]

    passo = max(1, int(meses))
    ano, mes = primeira.year, primeira.month
    multiplicador = 1
    while True:
        total = (ano * 12 + (mes - 1)) + multiplicador * passo
        alvo_ano, alvo_mes = divmod(total, 12)
        alvo = pd.Timestamp(year=alvo_ano, month=alvo_mes + 1, day=1)
        if alvo > limite:
            break
        escolhida = next((indice for indice in candidatas if indice >= alvo), None)
        if escolhida is None:
            break
        if escolhida not in decisões:
            decisões.append(escolhida)
        multiplicador += 1
    return decisões


def _retornos(valores: list[float]) -> list[float]:
    retornos: list[float] = []
    for anterior, atual in zip(valores, valores[1:]):
        if anterior and anterior > 0:
            retornos.append(atual / anterior - 1)
    return retornos


def _metricas(
    valores: list[float], base: float, inicio: date, fim: date
) -> dict[str, float]:
    if base <= 0:
        return {
            "retorno_total_pct": 0.0,
            "retorno_anualizado_pct": 0.0,
            "volatilidade_anual_pct": 0.0,
            "drawdown_maximo_pct": 0.0,
        }
    final = valores[-1] if valores else base
    total = (final / base - 1) * 100
    dias = max(1, (fim - inicio).days)
    if final > 0:
        anualizado = ((final / base) ** (365.25 / dias) - 1) * 100
    else:
        anualizado = -100.0
    retornos = _retornos(valores)
    volatilidade = (
        statistics.pstdev(retornos) * math.sqrt(252) * 100
        if len(retornos) >= 2
        else 0.0
    )
    return {
        "retorno_total_pct": float(total),
        "retorno_anualizado_pct": float(anualizado),
        "volatilidade_anual_pct": float(volatilidade),
        "drawdown_maximo_pct": _drawdown_maximo(valores),
    }


def _serie_benchmark(
    df: pd.DataFrame | None,
    calendario: list[pd.Timestamp],
    capital_inicial: float,
) -> list[float | None]:
    if df is None or df.empty:
        return [None for _ in calendario]
    serie = df["fechamento"].astype(float)
    serie = serie[~serie.index.duplicated(keep="last")].sort_index()
    completa = serie.reindex(sorted(set(calendario) | set(serie.index))).ffill()
    recorte = completa.reindex(calendario)
    validos = recorte.dropna()
    if validos.empty:
        return [None for _ in calendario]
    inicial = float(validos.iloc[0])
    if inicial <= 0:
        return [None for _ in calendario]
    return [
        None if pd.isna(valor) else float(capital_inicial * float(valor) / inicial)
        for valor in recorte
    ]


def simular(
    estrategia: str,
    inicio: date,
    fim: date,
    rebalanceamento_meses: int = 3,
    capital_inicial: float = 100_000,
    custo_bps: float = 10,
    top_n: int = 10,
    config: dict | None = None,
) -> dict[str, Any]:
    """Simula uma carteira com dados disponíveis até cada data de decisão."""

    nome = _normalizar_estrategia(estrategia)
    if nome not in ESTRATEGIAS_FUNDAMENTALISTAS | ESTRATEGIAS_PRECO:
        raise ValueError(f"estratégia desconhecida para backtest: {estrategia}")

    inicio = _data(inicio)
    fim = _data(fim)
    if fim < inicio:
        raise ValueError("fim anterior ao início")
    if capital_inicial <= 0:
        raise ValueError("capital inicial deve ser positivo")
    if top_n <= 0:
        raise ValueError("top_n deve ser positivo")

    configuracao = config if isinstance(config, dict) else estrategias.CONFIG_PADRAO
    benchmark_ticker = BENCHMARK_PADRAO
    limitacoes: list[str] = []
    inicio_pedido = inicio

    with db.conectar() as conn:
        ativos = _carregar_ativos(conn)
        cotacoes = _carregar_cotacoes(conn, fim)
        fundamentos, primeira_foto = _carregar_fundamentos(conn, fim)
        proventos = _carregar_proventos(conn, fim)
        primeira_cotacao = _primeira_cotacao(cotacoes, benchmark_ticker)
        primeira_foto_completa = _primeira_foto_completa(conn)

    limitacoes.append(
        "ativos deslistados só entram se ainda estiverem no banco: não há "
        "reconstrução do universo histórico completo."
    )
    limitacoes.append(
        "proventos entram como retorno na data ex, sem reinvestimento "
        "fracionário nem tributação: é uma aproximação."
    )

    inicio_efetivo = inicio
    if nome in ESTRATEGIAS_FUNDAMENTALISTAS:
        if primeira_foto is None:
            limitacoes.append(
                "sem foto de fundamentos no banco: esta estratégia não pode ser "
                "simulada."
            )
        elif primeira_foto_completa is not None and inicio < primeira_foto_completa:
            limitacoes.append(
                "sem foto completa de fundamentos antes de "
                f"{primeira_foto_completa.isoformat()}: o trecho anterior não é "
                "simulado"
            )
            inicio_efetivo = primeira_foto_completa
        elif inicio < primeira_foto:
            limitacoes.append(
                f"sem foto de fundamentos antes de {primeira_foto.isoformat()}: "
                "este trecho não é simulado"
            )
            inicio_efetivo = primeira_foto
        limitacoes.append(
            "fundamentos são a foto da coleta (data_ref = dia da coleta), não "
            "demonstrações históricas ponto-no-tempo; o passado anterior à "
            "primeira foto não pode ser reconstruído."
        )

    if primeira_cotacao and inicio_efetivo < primeira_cotacao:
        limitacoes.append(
            f"sem cotação antes de {primeira_cotacao.isoformat()}: o trecho "
            "anterior não é simulado."
        )
        if nome in ESTRATEGIAS_PRECO:
            inicio_efetivo = max(inicio_efetivo, primeira_cotacao)

    if inicio_efetivo >= fim:
        limitacoes.append(
            "sem período simulável: o início efetivo "
            f"{inicio_efetivo.isoformat()} não é anterior ao fim "
            f"{fim.isoformat()}; não há trecho para simular."
        )
        return {
            "estrategia": nome,
            "inicio": inicio_efetivo.isoformat(),
            "inicio_pedido": inicio_pedido.isoformat(),
            "fim": fim.isoformat(),
            "erro": "sem período simulável",
            "rebalanceamentos": 0,
            "valor_final": None,
            "retorno_total_pct": None,
            "retorno_anualizado_pct": None,
            "volatilidade_anual_pct": None,
            "drawdown_maximo_pct": None,
            "benchmark": {
                "ticker": benchmark_ticker,
                "retorno_total_pct": None,
                "retorno_anualizado_pct": None,
                "drawdown_maximo_pct": None,
            },
            "serie": [],
            "carteiras": [],
            "limitacoes": limitacoes,
            "cobertura": {
                "primeira_foto": primeira_foto.isoformat() if primeira_foto else None,
                "primeira_foto_completa": primeira_foto_completa.isoformat()
                if primeira_foto_completa
                else None,
                "primeira_cotacao": primeira_cotacao.isoformat()
                if primeira_cotacao
                else None,
            },
        }

    calendario_total = _calendario(cotacoes)
    calendario = [
        indice
        for indice in calendario_total
        if pd.Timestamp(inicio_efetivo) <= indice <= pd.Timestamp(fim)
    ]
    if not calendario:
        limitacoes.append("não há sessão de negociação no período escolhido.")
        return {
            "estrategia": nome,
            "inicio": inicio_efetivo.isoformat(),
            "inicio_pedido": inicio_pedido.isoformat(),
            "fim": fim.isoformat(),
            "erro": None,
            "rebalanceamentos": 0,
            "valor_final": float(capital_inicial),
            "retorno_total_pct": 0.0,
            "retorno_anualizado_pct": 0.0,
            "volatilidade_anual_pct": 0.0,
            "drawdown_maximo_pct": 0.0,
            "benchmark": {
                "ticker": benchmark_ticker,
                "retorno_total_pct": None,
                "retorno_anualizado_pct": None,
                "drawdown_maximo_pct": None,
            },
            "serie": [],
            "carteiras": [],
            "limitacoes": limitacoes,
            "cobertura": {
                "primeira_foto": primeira_foto.isoformat() if primeira_foto else None,
                "primeira_foto_completa": primeira_foto_completa.isoformat()
                if primeira_foto_completa
                else None,
                "primeira_cotacao": primeira_cotacao.isoformat()
                if primeira_cotacao
                else None,
            },
        }

    inicio_real = calendario[0].date()
    fim_real = calendario[-1].date()

    universo = sorted(
        ticker
        for ticker in set(ativos) | set(cotacoes)
        if ticker != benchmark_ticker and not ticker.startswith("^")
    )

    bench_df = cotacoes.get(benchmark_ticker)
    if bench_df is None or bench_df.empty:
        limitacoes.append(
            f"benchmark {benchmark_ticker} sem cotações no banco: a comparação "
            "fica indisponível."
        )
    serie_bench = _serie_benchmark(bench_df, calendario, capital_inicial)

    precos_ff: dict[str, pd.Series] = {}
    for ticker, df in cotacoes.items():
        if df.empty:
            continue
        serie = df["fechamento"].astype(float)
        serie = serie[~serie.index.duplicated(keep="last")].sort_index()
        completa = serie.reindex(sorted(set(calendario_total) | set(serie.index))).ffill()
        precos_ff[ticker] = completa.reindex(calendario)

    benchmark_hist: pd.Series | None = None
    if bench_df is not None and not bench_df.empty:
        benchmark_hist = bench_df["fechamento"].astype(float)
        benchmark_hist = benchmark_hist[
            ~benchmark_hist.index.duplicated(keep="last")
        ].sort_index()

    decisões = _datas_decisao(inicio_real, fim_real, rebalanceamento_meses, calendario)
    conjunto_decisoes = set(decisões)

    posicoes: dict[str, float] = {}
    caixa = float(capital_inicial)
    data_anterior = inicio_real - timedelta(days=1)
    serie: list[dict[str, Any]] = []
    carteiras: list[dict[str, Any]] = []
    valores: list[float] = []

    def preco(ticker: str, quando: pd.Timestamp) -> float | None:
        serie_ticker = precos_ff.get(ticker)
        if serie_ticker is None:
            return None
        valor = serie_ticker.get(quando)
        return None if pd.isna(valor) else float(valor)

    for quando in calendario:
        for ticker, quantidade in list(posicoes.items()):
            for data_ex, valor_provento in proventos.get(ticker, []):
                if data_anterior < data_ex <= quando.date():
                    caixa += quantidade * valor_provento

        if quando in conjunto_decisoes:
            valor_bruto = caixa
            for ticker, quantidade in posicoes.items():
                atual = preco(ticker, quando)
                if atual is not None:
                    valor_bruto += quantidade * atual

            pesos_atuais: dict[str, float] = {}
            if valor_bruto > 0:
                for ticker, quantidade in posicoes.items():
                    atual = preco(ticker, quando)
                    if atual is not None:
                        pesos_atuais[ticker] = quantidade * atual / valor_bruto

            selecionados, motivos = _selecionar(
                nome,
                quando,
                universo,
                cotacoes,
                ativos,
                fundamentos,
                proventos,
                benchmark_hist,
                configuracao,
                top_n,
            )
            if not selecionados:
                resumo = ", ".join(
                    f"{quantidade} {motivo}"
                    for motivo, quantidade in sorted(motivos.items())
                )
                if not resumo:
                    resumo = "nenhum ativo passou nos critérios"
                limitacoes.append(
                    "nenhum ativo elegível em "
                    f"{quando.date().isoformat()}: {resumo}"
                )

            pesos_novos = (
                {ticker: 1.0 / len(selecionados) for ticker in selecionados}
                if selecionados
                else {}
            )
            giro = sum(
                abs(pesos_novos.get(ticker, 0.0) - pesos_atuais.get(ticker, 0.0))
                for ticker in set(pesos_novos) | set(pesos_atuais)
            )
            custo = valor_bruto * (float(custo_bps) / 10_000.0) * giro
            valor_liquido = max(0.0, valor_bruto - custo)

            posicoes = {}
            caixa = valor_liquido
            if selecionados:
                for ticker in selecionados:
                    atual = preco(ticker, quando)
                    if atual is None or atual <= 0:
                        continue
                    alocacao = valor_liquido * pesos_novos[ticker]
                    quantidade = alocacao / atual
                    posicoes[ticker] = quantidade
                    caixa -= alocacao

            carteiras.append(
                {
                    "data": quando.date().isoformat(),
                    "tickers": list(selecionados),
                    "pesos": [
                        float(pesos_novos[ticker]) for ticker in selecionados
                    ],
                }
            )
            valor = caixa
            for ticker, quantidade in posicoes.items():
                atual = preco(ticker, quando)
                if atual is not None:
                    valor += quantidade * atual
        else:
            valor = caixa
            for ticker, quantidade in posicoes.items():
                atual = preco(ticker, quando)
                if atual is not None:
                    valor += quantidade * atual

        serie.append(
            {
                "data": quando.date().isoformat(),
                "carteira": float(valor),
                "benchmark": serie_bench[len(serie)],
            }
        )
        valores.append(float(valor))
        data_anterior = quando.date()

    if carteiras and all(not carteira["tickers"] for carteira in carteiras):
        limitacoes.insert(0, "simulação sem carteira: verifique elegibilidade")

    metricas = _metricas(valores, float(capital_inicial), inicio_real, fim_real)

    bench_validos = [valor for valor in serie_bench if valor is not None]
    if bench_validos:
        metricas_bench = _metricas(
            [float(valor) for valor in bench_validos],
            float(bench_validos[0]),
            inicio_real,
            fim_real,
        )
        benchmark = {
            "ticker": benchmark_ticker,
            "retorno_total_pct": metricas_bench["retorno_total_pct"],
            "retorno_anualizado_pct": metricas_bench["retorno_anualizado_pct"],
            "drawdown_maximo_pct": metricas_bench["drawdown_maximo_pct"],
        }
    else:
        benchmark = {
            "ticker": benchmark_ticker,
            "retorno_total_pct": None,
            "retorno_anualizado_pct": None,
            "drawdown_maximo_pct": None,
        }

    return {
        "estrategia": nome,
        "inicio": inicio_real.isoformat(),
        "inicio_pedido": inicio_pedido.isoformat(),
        "fim": fim_real.isoformat(),
        "erro": None,
        "rebalanceamentos": len(carteiras),
        "valor_final": float(valores[-1]) if valores else float(capital_inicial),
        "retorno_total_pct": metricas["retorno_total_pct"],
        "retorno_anualizado_pct": metricas["retorno_anualizado_pct"],
        "volatilidade_anual_pct": metricas["volatilidade_anual_pct"],
        "drawdown_maximo_pct": metricas["drawdown_maximo_pct"],
        "benchmark": benchmark,
        "serie": serie,
        "carteiras": carteiras,
        "limitacoes": limitacoes,
        "cobertura": {
            "primeira_foto": primeira_foto.isoformat() if primeira_foto else None,
            "primeira_foto_completa": primeira_foto_completa.isoformat()
            if primeira_foto_completa
            else None,
            "primeira_cotacao": primeira_cotacao.isoformat() if primeira_cotacao else None,
        },
    }
