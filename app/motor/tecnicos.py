"""Indicadores técnicos puros, com a periodicidade explícita no nome.

As séries de fechamento chegam indexadas por ``DatetimeIndex``. Funções que
misturam barras diárias e semanas declaram isso no nome (ex.: ``sma_semanal``)
e nas colunas (sufixo ``s``), nunca na mesma linha sem rótulo.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

DIAS_52_SEMANAS = 252
JANELA_MES_DIAS = 21


def _indice_datetime(df: pd.DataFrame) -> pd.DataFrame:
    trabalho = df.copy()
    if "data" in trabalho.columns:
        trabalho["data"] = pd.to_datetime(trabalho["data"], errors="coerce")
        trabalho = trabalho.dropna(subset=["data"]).set_index("data")
    if not isinstance(trabalho.index, pd.DatetimeIndex):
        trabalho.index = pd.to_datetime(trabalho.index, errors="coerce")
        trabalho = trabalho[~trabalho.index.isna()]
    return trabalho.sort_index()


def _serie(cotacoes: Any, coluna: str = "fechamento") -> pd.Series:
    if isinstance(cotacoes, pd.Series):
        serie = cotacoes.copy()
    elif isinstance(cotacoes, pd.DataFrame):
        df = _indice_datetime(cotacoes)
        if coluna not in df.columns:
            return pd.Series(dtype="float64")
        serie = df[coluna]
    else:
        raise TypeError("esperado pd.Series ou pd.DataFrame com DatetimeIndex")

    serie = pd.to_numeric(serie, errors="coerce").dropna()
    if not isinstance(serie.index, pd.DatetimeIndex):
        serie.index = pd.to_datetime(serie.index, errors="coerce")
        serie = serie[~serie.index.isna()]
    if getattr(serie.index, "tz", None) is not None:
        serie.index = serie.index.tz_localize(None)
    serie = serie[~serie.index.duplicated(keep="last")]
    return serie.sort_index()


def _df_ohlcv(cotacoes: pd.DataFrame) -> pd.DataFrame:
    df = _indice_datetime(cotacoes)
    fechamento = pd.to_numeric(df.get("fechamento"), errors="coerce")
    for coluna in ("abertura", "maxima", "minima", "fechamento", "volume"):
        if coluna in df.columns:
            df[coluna] = pd.to_numeric(df[coluna], errors="coerce")
        elif coluna == "volume":
            df[coluna] = 0.0
        else:
            df[coluna] = fechamento
    return df


def resumo_semanal(cotacoes: pd.DataFrame) -> pd.DataFrame:
    """Agrega barras diárias em semanas encerradas na sexta.

    ``abertura`` = primeiro, ``maxima`` = máximo, ``minima`` = mínimo,
    ``fechamento`` = último e ``volume`` = soma. A semana corrente (ou qualquer
    semana cuja sexta ainda não chegou) é devolvida com ``completa=False``.
    """

    df = _df_ohlcv(cotacoes)
    if df.empty:
        return pd.DataFrame(
            columns=["abertura", "maxima", "minima", "fechamento", "volume", "completa"]
        )

    semanal = df.resample("W-FRI").agg(
        {
            "abertura": "first",
            "maxima": "max",
            "minima": "min",
            "fechamento": "last",
            "volume": "sum",
        }
    )
    semanal = semanal.dropna(subset=["fechamento"])
    hoje = pd.Timestamp.today().normalize()
    semanal["completa"] = [indice.normalize() <= hoje for indice in semanal.index]
    return semanal


def medias_moveis(
    serie: pd.Series,
    janelas: tuple[int, ...] | list[int] = (9, 21, 50, 200),
    tipo: str = "simples",
) -> pd.DataFrame:
    """Médias móveis da série, em dias, com colunas ``smaN`` ou ``emaN``."""

    fechamento = _serie(serie)
    tipo = (tipo or "simples").strip().lower()
    if tipo in {"simples", "sma"}:
        prefixo = "sma"
        calcular = lambda janela: fechamento.rolling(window=janela, min_periods=janela).mean()
    elif tipo in {"ema", "exponencial"}:
        prefixo = "ema"
        calcular = lambda janela: fechamento.ewm(span=janela, adjust=False).mean()
    else:
        raise ValueError(f"tipo de média desconhecido: {tipo}")

    resultado = pd.DataFrame(index=fechamento.index)
    for janela in janelas:
        inteiro = int(janela)
        if inteiro <= 0:
            raise ValueError("janela deve ser positiva")
        resultado[f"{prefixo}{inteiro}"] = calcular(inteiro)
    return resultado


def bollinger(
    serie: pd.Series | pd.DataFrame, janela: int = 20, desvios: float = 2.0
) -> pd.DataFrame:
    """Bandas de Bollinger da série: ``media``, ``superior`` e ``inferior``.

    A janela conta na periodicidade da série recebida (dias, semanas ou meses),
    então só deve ser chamada com a série já agregada. Desvio populacional
    (``ddof=0``), como na definição clássica.
    """

    fechamento = _serie(serie)
    inteiro = int(janela)
    media = fechamento.rolling(window=inteiro, min_periods=inteiro).mean()
    desvio = fechamento.rolling(window=inteiro, min_periods=inteiro).std(ddof=0)
    return pd.DataFrame(
        {
            "media": media,
            "superior": media + desvios * desvio,
            "inferior": media - desvios * desvio,
        }
    )


def sma_semanal(
    cotacoes: pd.DataFrame,
    janelas: tuple[int, ...] | list[int] = (9, 21, 100, 150),
) -> pd.DataFrame:
    """Médias móveis de **semanas** sobre o fechamento semanal.

    As colunas têm sufixo ``s`` (``sma9s``, ``sma21s``, ...). O primeiro valor
    de ``sma9s`` aparece somente na 9ª semana e usa 9 fechamentos semanais.
    """

    semanal = resumo_semanal(cotacoes)
    if semanal.empty:
        return pd.DataFrame(columns=[f"sma{int(j)}s" for j in janelas])

    fechamento = semanal["fechamento"]
    resultado = pd.DataFrame(index=semanal.index)
    for janela in janelas:
        inteiro = int(janela)
        resultado[f"sma{inteiro}s"] = fechamento.rolling(
            window=inteiro, min_periods=inteiro
        ).mean()
    return resultado


def _criterio(atende: bool | None, valor: float | None, regra: str) -> dict[str, Any]:
    return {
        "atende": None if atende is None else bool(atende),
        "valor": None if valor is None else float(valor),
        "regra": regra,
    }


def _extremos_52s(cotacoes: pd.DataFrame) -> tuple[float | None, float | None]:
    if isinstance(cotacoes, pd.Series):
        serie = _serie(cotacoes)
        if len(serie) < DIAS_52_SEMANAS:
            return None, None
        janela = serie.iloc[-DIAS_52_SEMANAS:]
        return float(janela.max()), float(janela.min())
    df = _df_ohlcv(cotacoes)
    if len(df) < DIAS_52_SEMANAS:
        return None, None
    janela = df.iloc[-DIAS_52_SEMANAS:]
    maxima = janela["maxima"].max()
    minima = janela["minima"].min()
    if pd.isna(maxima) or pd.isna(minima):
        return None, None
    return float(maxima), float(minima)


def trend_template(cotacoes: pd.DataFrame) -> dict[str, Any]:
    """Critérios da metodologia de tendência, calculados em **dias**.

    Cada critério devolve ``atende``/``valor``/``regra``. Sem histórico
    suficiente o critério fica ``None`` (indisponível), nunca ``False``.
    """

    fechamento = _serie(cotacoes)
    preco = float(fechamento.iloc[-1]) if len(fechamento) else None
    sma50 = fechamento.rolling(50, min_periods=50).mean()
    sma150 = fechamento.rolling(150, min_periods=150).mean()
    sma200 = fechamento.rolling(200, min_periods=200).mean()

    s50 = float(sma50.iloc[-1]) if len(sma50) and not pd.isna(sma50.iloc[-1]) else None
    s150 = float(sma150.iloc[-1]) if len(sma150) and not pd.isna(sma150.iloc[-1]) else None
    s200 = float(sma200.iloc[-1]) if len(sma200) and not pd.isna(sma200.iloc[-1]) else None

    s200_antes = None
    if len(sma200) > JANELA_MES_DIAS:
        valor_antes = sma200.iloc[-JANELA_MES_DIAS - 1]
        if not pd.isna(valor_antes):
            s200_antes = float(valor_antes)

    criterios: dict[str, dict[str, Any]] = {}

    criterios["preco_acima_sma200"] = _criterio(
        None if preco is None or s200 is None else preco > s200,
        s200,
        "preço de fechamento acima da SMA200 diária",
    )
    criterios["sma150_acima_sma200"] = _criterio(
        None if s150 is None or s200 is None else s150 > s200,
        s150,
        "SMA150 diária acima da SMA200 diária",
    )

    variacao_200 = None
    if s200 is not None and s200_antes is not None:
        variacao_200 = s200 - s200_antes
    criterios["sma200_subindo"] = _criterio(
        None if variacao_200 is None else variacao_200 > 0,
        variacao_200,
        "SMA200 diária acima do valor de um mês atrás",
    )
    criterios["sma50_acima_sma150_sma200"] = _criterio(
        None if s50 is None or s150 is None or s200 is None else (s50 > s150 and s50 > s200),
        s50,
        "SMA50 diária acima da SMA150 e da SMA200",
    )
    criterios["preco_acima_sma50"] = _criterio(
        None if preco is None or s50 is None else preco > s50,
        s50,
        "preço de fechamento acima da SMA50 diária",
    )

    maxima, minima = _extremos_52s(cotacoes)
    distancia_maxima = None
    if preco is not None and maxima is not None and maxima > 0:
        distancia_maxima = (maxima - preco) / maxima * 100
    criterios["proximo_maxima_52s"] = _criterio(
        None if distancia_maxima is None else distancia_maxima <= 25.0,
        distancia_maxima,
        "preço a no máximo 25% da máxima de 52 semanas",
    )

    acima_minima = None
    if preco is not None and minima is not None and minima > 0:
        acima_minima = (preco / minima - 1) * 100
    criterios["acima_minima_52s"] = _criterio(
        None if acima_minima is None else acima_minima >= 25.0,
        acima_minima,
        "preço a pelo menos 25% acima da mínima de 52 semanas",
    )

    disponiveis = [item for item in criterios.values() if item["atende"] is not None]
    if len(disponiveis) < len(criterios):
        atende_todos = None
    else:
        atende_todos = all(item["atende"] for item in disponiveis)

    criterios["atende_todos"] = atende_todos
    criterios["criterios_disponiveis"] = len(disponiveis)
    return criterios


def distancia_maxima_52s(cotacoes: pd.DataFrame) -> float | None:
    """Distância percentual do preço até a máxima de 52 semanas."""

    fechamento = _serie(cotacoes)
    if not len(fechamento):
        return None
    maxima, _ = _extremos_52s(cotacoes)
    if maxima is None or maxima <= 0:
        return None
    preco = float(fechamento.iloc[-1])
    return (maxima - preco) / maxima * 100


def forca_relativa(cotacoes: pd.DataFrame, benchmark: pd.DataFrame | pd.Series) -> dict[str, Any]:
    """Retornos do ativo e do benchmark em 1/3/6/12 meses, alinhados por data.

    Devolve ``ativo_Nm``, ``benchmark_Nm`` e ``rs_Nm`` (diferença em pontos
    percentuais). O alinhamento é feito pela interseção de datas, nunca pela
    posição das linhas.
    """

    ativo = _serie(cotacoes)
    referencia = _serie(benchmark)
    dados = pd.concat(
        [ativo.rename("ativo"), referencia.rename("benchmark")], axis=1, join="inner"
    ).dropna()
    resultado: dict[str, Any] = {}
    periodos = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}

    if len(dados) < 260:
        raise ValueError(
            "benchmark precisa de pelo menos 260 barras na mesma janela do ativo "
            f"para calcular a força relativa (encontradas {len(dados)})"
        )

    if len(dados) < 2:
        for rotulo in periodos:
            resultado[f"ativo_{rotulo}"] = None
            resultado[f"benchmark_{rotulo}"] = None
            resultado[f"rs_{rotulo}"] = None
        return resultado

    fim = dados.index[-1]
    for rotulo, meses in periodos.items():
        inicio = fim - pd.DateOffset(months=meses)
        posicao = int(dados.index.searchsorted(inicio, side="right")) - 1
        if posicao < 0 or posicao >= len(dados) - 1:
            resultado[f"ativo_{rotulo}"] = None
            resultado[f"benchmark_{rotulo}"] = None
            resultado[f"rs_{rotulo}"] = None
            continue

        preco_inicial_ativo = dados["ativo"].iloc[posicao]
        preco_final_ativo = dados["ativo"].iloc[-1]
        preco_inicial_bench = dados["benchmark"].iloc[posicao]
        preco_final_bench = dados["benchmark"].iloc[-1]

        if preco_inicial_ativo <= 0 or preco_inicial_bench <= 0:
            resultado[f"ativo_{rotulo}"] = None
            resultado[f"benchmark_{rotulo}"] = None
            resultado[f"rs_{rotulo}"] = None
            continue

        retorno_ativo = (preco_final_ativo / preco_inicial_ativo - 1) * 100
        retorno_bench = (preco_final_bench / preco_inicial_bench - 1) * 100
        resultado[f"ativo_{rotulo}"] = float(retorno_ativo)
        resultado[f"benchmark_{rotulo}"] = float(retorno_bench)
        resultado[f"rs_{rotulo}"] = float(retorno_ativo - retorno_bench)

    return resultado


def sinais(cotacoes: pd.DataFrame) -> list[dict[str, Any]]:
    """Sinais objetivos com explicação. Sem dado → ``atende=None``."""

    fechamento = _serie(cotacoes)
    volume = _serie(cotacoes, "volume")
    preco = float(fechamento.iloc[-1]) if len(fechamento) else None
    sma50 = fechamento.rolling(50, min_periods=50).mean()
    sma200 = fechamento.rolling(200, min_periods=200).mean()

    s50 = float(sma50.iloc[-1]) if len(sma50) and not pd.isna(sma50.iloc[-1]) else None
    s200 = float(sma200.iloc[-1]) if len(sma200) and not pd.isna(sma200.iloc[-1]) else None
    s200_antes = None
    if len(sma200) > JANELA_MES_DIAS:
        candidato = sma200.iloc[-JANELA_MES_DIAS - 1]
        if not pd.isna(candidato):
            s200_antes = float(candidato)
    subindo = None if s200 is None or s200_antes is None else s200 > s200_antes
    caindo = None if s200 is None or s200_antes is None else s200 < s200_antes

    lista: list[dict[str, Any]] = []

    if preco is None or s50 is None or s200 is None or subindo is None:
        lista.append(
            {
                "sinal": "tendencia_alta",
                "atende": None,
                "motivo": "histórico insuficiente",
            }
        )
        lista.append(
            {
                "sinal": "tendencia_baixa",
                "atende": None,
                "motivo": "histórico insuficiente",
            }
        )
    else:
        alta = preco > s50 > s200 and subindo
        baixa = preco < s50 < s200 and bool(caindo)
        lista.append(
            {
                "sinal": "tendencia_alta",
                "atende": bool(alta),
                "motivo": "fechamento > SMA50 > SMA200 e SMA200 subindo",
            }
        )
        lista.append(
            {
                "sinal": "tendencia_baixa",
                "atende": bool(baixa),
                "motivo": "fechamento < SMA50 < SMA200 e SMA200 caindo",
            }
        )

    if len(fechamento) < 60:
        lista.append(
            {
                "sinal": "consolidacao",
                "atende": None,
                "motivo": "histórico insuficiente",
            }
        )
    else:
        janela = fechamento.iloc[-60:]
        minimo = float(janela.min())
        maximo = float(janela.max())
        amplitude = (maximo - minimo) / minimo * 100 if minimo > 0 else None
        lista.append(
            {
                "sinal": "consolidacao",
                "atende": None if amplitude is None else amplitude < 8.0,
                "motivo": "amplitude dos últimos 60 dias"
                if amplitude is not None
                else "histórico insuficiente",
            }
        )

    distancia = distancia_maxima_52s(cotacoes)
    lista.append(
        {
            "sinal": "proximo_maxima",
            "atende": None if distancia is None else distancia <= 3.0,
            "motivo": "a até 3% da máxima de 52 semanas"
            if distancia is not None
            else "histórico insuficiente",
        }
    )

    if len(volume) < 50 or preco is None:
        lista.append(
            {
                "sinal": "volume_acima_media",
                "atende": None,
                "motivo": "histórico insuficiente",
            }
        )
    else:
        media = float(volume.iloc[-50:].mean())
        ultimo = float(volume.iloc[-1])
        lista.append(
            {
                "sinal": "volume_acima_media",
                "atende": None if media <= 0 else ultimo > 1.5 * media,
                "motivo": "volume do último dia acima de 1,5× a média de 50 dias"
                if media > 0
                else "volume sem histórico válido",
            }
        )

    cruzamento = None
    if len(fechamento) > 55 and s50 is not None and not pd.isna(sma50.iloc[-1]):
        recentes = fechamento.iloc[-5:]
        for data in recentes.index:
            posicao = fechamento.index.get_loc(data)
            if posicao <= 0:
                continue
            media_atual = sma50.iloc[posicao]
            media_anterior = sma50.iloc[posicao - 1]
            if pd.isna(media_atual) or pd.isna(media_anterior):
                continue
            if fechamento.iloc[posicao] > media_atual and fechamento.iloc[posicao - 1] <= media_anterior:
                cruzamento = True
                break
        if cruzamento is None:
            cruzamento = False

    lista.append(
        {
            "sinal": "recuperacao_media",
            "atende": cruzamento,
            "motivo": "fechamento cruzou acima da SMA50 nos últimos 5 dias"
            if cruzamento is not None
            else "histórico insuficiente",
        }
    )

    return lista
