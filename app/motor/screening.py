"""Screening diário com as médias de 9 e 21.

As funções de cálculo são puras e recebem ``pd.Series``/``pd.DataFrame``. A
única função com I/O é :func:`triar`, que lê os ativos e as cotações do banco e
mantém um cache curto em memória para a tela não recalcular tudo a cada clique.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import pandas as pd

from app.motor import tecnicos
from app.motor.estrategias import CONFIG_PADRAO

CACHE_SEGUNDOS = 300.0
_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, list[dict[str, Any]]]]] = {}


def _media(serie: pd.Series | pd.DataFrame, janela: int, tipo: str) -> pd.Series:
    janela = int(janela)
    if janela <= 0:
        raise ValueError("janela deve ser positiva")
    fechamento = tecnicos._serie(serie)
    tipo = (tipo or "ema").strip().lower()
    if tipo in {"ema", "exponencial"}:
        return fechamento.ewm(
            alpha=2 / (janela + 1), adjust=False, min_periods=janela
        ).mean()
    if tipo in {"sma", "simples"}:
        return fechamento.rolling(window=janela, min_periods=janela).mean()
    raise ValueError(f"tipo de média desconhecido: {tipo}")


def ema(serie: pd.Series | pd.DataFrame, janela: int) -> pd.Series:
    """EMA alinhada ao índice da série, com ``alpha = 2/(n+1)``."""

    return _media(serie, janela, "ema")


def sma(serie: pd.Series | pd.DataFrame, janela: int) -> pd.Series:
    """SMA alinhada ao índice da série (primeiro valor na enésima barra)."""

    return _media(serie, janela, "sma")


def _data(valor: Any) -> date | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, pd.Timestamp):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        return pd.Timestamp(valor).date()
    except (TypeError, ValueError):
        return None


def cruzamentos(
    serie: pd.Series | pd.DataFrame, janela_dias: int = 10, tipo: str = "ema"
) -> dict[str, Any]:
    """Última troca de sinal de ``M9 - M21`` dentro dos últimos pregões.

    Devolve somente o lado da troca mais recente: ``alta`` quando a diferença
    passa de ``<= 0`` para ``> 0`` e ``baixa`` no sentido inverso.
    """

    fechamento = tecnicos._serie(serie)
    if len(fechamento) < 2:
        return {"alta": None, "baixa": None, "dias_desde": None}

    diferenca = _media(fechamento, 9, tipo) - _media(fechamento, 21, tipo)
    janela = max(0, int(janela_dias))
    inicio = max(1, len(diferenca) - 1 - janela)

    ultimo: tuple[str, int] | None = None
    for posicao in range(inicio, len(diferenca)):
        anterior = diferenca.iloc[posicao - 1]
        atual = diferenca.iloc[posicao]
        if pd.isna(anterior) or pd.isna(atual):
            continue
        if anterior <= 0 < atual:
            ultimo = ("alta", posicao)
        elif anterior >= 0 > atual:
            ultimo = ("baixa", posicao)

    if ultimo is None:
        return {"alta": None, "baixa": None, "dias_desde": None}

    lado, posicao = ultimo
    data_troca = _data(diferenca.index[posicao])
    return {
        "alta": data_troca if lado == "alta" else None,
        "baixa": data_troca if lado == "baixa" else None,
        "dias_desde": int(len(diferenca) - 1 - posicao),
    }


def _subindo(serie: pd.Series | None, olhar: int) -> bool:
    if serie is None or len(serie) <= olhar:
        return False
    atual = serie.iloc[-1]
    anterior = serie.iloc[-1 - int(olhar)]
    if pd.isna(atual) or pd.isna(anterior):
        return False
    return bool(atual > anterior)


def _caindo(serie: pd.Series | None, olhar: int) -> bool:
    if serie is None or len(serie) <= olhar:
        return False
    atual = serie.iloc[-1]
    anterior = serie.iloc[-1 - int(olhar)]
    if pd.isna(atual) or pd.isna(anterior):
        return False
    return bool(atual < anterior)


def alinhamento(
    preco: float | None,
    m9: float | None,
    m21: float | None,
    serie_m9: pd.Series,
    serie_m21: pd.Series,
    olhar: int = 5,
) -> dict[str, bool]:
    """Preço, M9 e M21 empilhados e as duas médias subindo/descendo."""

    olhar = max(1, int(olhar))
    m9_subindo = _subindo(serie_m9, olhar)
    m21_subindo = _subindo(serie_m21, olhar)
    m9_caindo = _caindo(serie_m9, olhar)
    m21_caindo = _caindo(serie_m21, olhar)

    if preco is None or m9 is None or m21 is None:
        alta_forte = False
        baixa_forte = False
    else:
        alta_forte = bool(preco > m9 > m21 and m9_subindo and m21_subindo)
        baixa_forte = bool(preco < m9 < m21 and m9_caindo and m21_caindo)

    return {
        "alta_forte": alta_forte,
        "baixa_forte": baixa_forte,
        "m9_subindo": m9_subindo,
        "m21_subindo": m21_subindo,
    }


def pullback(
    df: pd.DataFrame,
    m9: float | None,
    m21: float | None,
    tolerancia_pct: float = 1.5,
    janela: int = 3,
) -> dict[str, Any] | None:
    """Candle recente que tocou a M9 ou a M21 numa tendência de alta.

    Só olha quando ``m9 > m21``. O candle precisa ter a mínima dentro da faixa
    da média (``minima <= media * (1 + tolerância)``) e fechar acima dela.
    """

    if m9 is None or m21 is None or not m9 > m21:
        return None

    trabalho = tecnicos._df_ohlcv(df)
    if trabalho.empty:
        return None

    tolerancia = max(0.0, float(tolerancia_pct))
    janela = max(1, int(janela))
    inicio = max(0, len(trabalho) - janela)

    for posicao in range(len(trabalho) - 1, inicio - 1, -1):
        linha = trabalho.iloc[posicao]
        abertura = linha["abertura"]
        minima = linha["minima"]
        fechamento = linha["fechamento"]
        if pd.isna(abertura) or pd.isna(minima) or pd.isna(fechamento):
            continue

        escolhida: tuple[str, float] | None = None
        for nome, media in (("m9", m9), ("m21", m21)):
            if media is None or media <= 0:
                continue
            if minima <= media * (1 + tolerancia / 100) and fechamento > media:
                distancia = abs(float(minima) - float(media)) / float(media) * 100
                if escolhida is None or distancia < escolhida[1]:
                    escolhida = (nome, distancia)

        if escolhida is None:
            continue

        return {
            "media_tocada": escolhida[0],
            "data": _data(trabalho.index[posicao]),
            "distancia_pct": float(escolhida[1]),
            "candle_reversao": bool(fechamento > abertura),
        }

    return None


def esticado(
    preco: float | None,
    m9: float | None,
    m21: float | None,
    limiar_preco_m9_pct: float = 8.0,
    limiar_m9_m21_pct: float = 4.0,
) -> dict[str, Any]:
    """Distância do preço à M9 e afastamento entre M9 e M21."""

    dist_preco_m9 = None
    spread_m9_m21 = None
    if preco is not None and m9 is not None and m9 > 0:
        dist_preco_m9 = float((preco / m9 - 1) * 100)
    if m9 is not None and m21 is not None and m21 > 0:
        spread_m9_m21 = float((m9 / m21 - 1) * 100)

    motivos: list[str] = []
    if dist_preco_m9 is not None and dist_preco_m9 >= float(limiar_preco_m9_pct):
        motivos.append(
            f"preço {dist_preco_m9:+.1f}% acima da M9 "
            f"(limiar {float(limiar_preco_m9_pct):g}%)"
        )
    if spread_m9_m21 is not None and spread_m9_m21 >= float(limiar_m9_m21_pct):
        motivos.append(
            f"spread M9-M21 {spread_m9_m21:+.1f}% "
            f"(limiar {float(limiar_m9_m21_pct):g}%)"
        )

    return {
        "dist_preco_m9_pct": dist_preco_m9,
        "spread_m9_m21_pct": spread_m9_m21,
        "esticado": bool(motivos),
        "motivo": "; ".join(motivos) if motivos else None,
    }


def _iso_data(valor: Any) -> str | None:
    convertida = _data(valor)
    return convertida.isoformat() if convertida is not None else None


def avaliar_ativo(
    ticker: str, classe: str | None, df: pd.DataFrame, config: dict[str, Any]
) -> dict[str, Any]:
    """Avalia um ativo nas quatro famílias do screening."""

    cfg = config or {}
    tipo = str(cfg.get("tipo") or "ema")
    historico_minimo = int(cfg.get("historico_minimo") or 60)
    janela_cruzamento = int(cfg.get("janela_cruzamento_dias") or 10)
    tolerancia = float(cfg.get("tolerancia_pullback_pct") or 1.5)
    janela_pullback = int(cfg.get("janela_pullback_dias") or 3)
    limiar_preco = float(cfg.get("limiar_preco_m9_pct") or 8.0)
    limiar_spread = float(cfg.get("limiar_m9_m21_pct") or 4.0)

    fechamento = tecnicos._serie(df)
    data_ref = _iso_data(fechamento.index[-1]) if len(fechamento) else None
    preco = float(fechamento.iloc[-1]) if len(fechamento) else None

    base: dict[str, Any] = {
        "ticker": str(ticker).upper(),
        "classe": classe,
        "data_ref": data_ref,
        "preco": preco,
        "m9": None,
        "m21": None,
        "dist_preco_m9_pct": None,
        "spread_m9_m21_pct": None,
        "cruzamento_alta": None,
        "cruzamento_baixa": None,
        "dias_desde_cruzamento": None,
        "alinhamento_alta_forte": False,
        "alinhamento_baixa_forte": False,
        "pullback_alta": None,
        "esticado": False,
        "motivos": [],
        "disponivel": False,
    }

    if len(fechamento) < historico_minimo:
        base["motivos"] = [
            f"histórico insuficiente: {len(fechamento)} barras "
            f"(mínimo {historico_minimo})"
        ]
        return base

    serie_m9 = _media(fechamento, 9, tipo)
    serie_m21 = _media(fechamento, 21, tipo)
    if serie_m9.dropna().empty or serie_m21.dropna().empty:
        base["motivos"] = ["médias indisponíveis para o histórico disponível"]
        return base

    m9 = float(serie_m9.iloc[-1])
    m21 = float(serie_m21.iloc[-1])
    if pd.isna(m9) or pd.isna(m21):
        base["motivos"] = ["médias indisponíveis para o histórico disponível"]
        return base

    troca = cruzamentos(fechamento, janela_cruzamento, tipo)
    alinhado = alinhamento(preco, m9, m21, serie_m9, serie_m21)
    toque = pullback(df, m9, m21, tolerancia, janela_pullback)
    esticamento = esticado(preco, m9, m21, limiar_preco, limiar_spread)

    motivos: list[str] = []
    if troca["alta"] is not None:
        motivos.append(f"cruzamento de alta em {_iso_data(troca['alta'])}")
    if troca["baixa"] is not None:
        motivos.append(f"cruzamento de baixa em {_iso_data(troca['baixa'])}")
    if alinhado["alta_forte"]:
        motivos.append("alinhamento de alta forte (preço > M9 > M21 e médias subindo)")
    if alinhado["baixa_forte"]:
        motivos.append("alinhamento de baixa forte (preço < M9 < M21 e médias caindo)")
    if toque is not None:
        motivos.append(
            f"pullback na {toque['media_tocada'].upper()} em {_iso_data(toque['data'])}"
        )
    if esticamento["motivo"]:
        motivos.append(esticamento["motivo"])

    if toque is not None:
        toque = {**toque, "data": _iso_data(toque["data"])}

    base.update(
        {
            "m9": m9,
            "m21": m21,
            "dist_preco_m9_pct": esticamento["dist_preco_m9_pct"],
            "spread_m9_m21_pct": esticamento["spread_m9_m21_pct"],
            "cruzamento_alta": _iso_data(troca["alta"]),
            "cruzamento_baixa": _iso_data(troca["baixa"]),
            "dias_desde_cruzamento": troca["dias_desde"],
            "alinhamento_alta_forte": bool(alinhado["alta_forte"]),
            "alinhamento_baixa_forte": bool(alinhado["baixa_forte"]),
            "pullback_alta": toque,
            "esticado": bool(esticamento["esticado"]),
            "motivo_esticado": esticamento["motivo"],
            "motivos": motivos,
            "disponivel": True,
        }
    )
    return base


def _carregar_ativos(classe: str | None = None) -> list[tuple[str, str | None, pd.DataFrame]]:
    """Lê do banco os ativos e as cotações diárias."""

    from app import db

    sql = "SELECT ticker, classe FROM ativos"
    parametros: list[Any] = []
    if classe:
        sql += " WHERE classe = ?"
        parametros.append(classe)
    sql += " ORDER BY ticker"

    with db.conectar() as conn:
        ativos = conn.execute(sql, parametros).fetchall()
        resultado: list[tuple[str, str | None, pd.DataFrame]] = []
        for ativo in ativos:
            ticker = ativo["ticker"]
            linhas = conn.execute(
                """
                SELECT data, abertura, maxima, minima, fechamento, volume
                  FROM cotacoes
                 WHERE ticker = ?
                 ORDER BY data
                """,
                (ticker,),
            ).fetchall()
            if not linhas:
                continue
            df = pd.DataFrame([{chave: linha[chave] for chave in linha.keys()} for linha in linhas])
            resultado.append((ticker, ativo["classe"], df))
    return resultado


def _chave_cache(config: dict[str, Any], classe: str | None) -> tuple[Any, ...]:
    from app import config as config_app

    campos = (
        "tipo",
        "janela_cruzamento_dias",
        "tolerancia_pullback_pct",
        "janela_pullback_dias",
        "limiar_preco_m9_pct",
        "limiar_m9_m21_pct",
        "historico_minimo",
    )
    return (
        classe,
        str(config_app.DB_PATH),
    ) + tuple(config.get(campo) for campo in campos)


def limpar_cache() -> None:
    """Esvazia o cache em memória (usado pela tela após atualização manual)."""

    _CACHE.clear()


def _ordenar_cruzamento(item: dict[str, Any]) -> tuple[int, str]:
    dias = item.get("dias_desde_cruzamento")
    return (int(dias) if dias is not None else 10**9, str(item.get("ticker") or ""))


def _ordenar_alinhamento(item: dict[str, Any]) -> tuple[float, str]:
    distancia = item.get("dist_preco_m9_pct")
    return (-(float(distancia) if distancia is not None else -10**9), str(item.get("ticker") or ""))


def _ordenar_pullback(item: dict[str, Any]) -> tuple[float, str]:
    toque = item.get("pullback_alta") or {}
    distancia = toque.get("distancia_pct")
    return (
        float(distancia) if distancia is not None else 10**9,
        str(item.get("ticker") or ""),
    )


def _ordenar_esticado(item: dict[str, Any]) -> tuple[float, str]:
    spread = item.get("spread_m9_m21_pct")
    return (
        -(float(spread) if spread is not None else -10**9),
        str(item.get("ticker") or ""),
    )


def triar(
    config: dict[str, Any] | None = None, classe: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Monta as seis listas do screening, ordenadas por mérito."""

    cfg = {**CONFIG_PADRAO["screening"], **(config or {})}
    chave = _chave_cache(cfg, classe)
    agora = time.monotonic()
    em_cache = _CACHE.get(chave)
    if em_cache is not None and agora - em_cache[0] < CACHE_SEGUNDOS:
        return em_cache[1]

    listas: dict[str, list[dict[str, Any]]] = {
        "cruzamento_alta": [],
        "cruzamento_baixa": [],
        "alinhamento_alta": [],
        "alinhamento_baixa": [],
        "pullback_alta": [],
        "esticado": [],
    }

    for ticker, classe_ativo, df in _carregar_ativos(classe):
        avaliado = avaliar_ativo(ticker, classe_ativo, df, cfg)
        if not avaliado.get("disponivel"):
            continue

        if avaliado.get("cruzamento_alta"):
            listas["cruzamento_alta"].append(avaliado)
        if avaliado.get("cruzamento_baixa"):
            listas["cruzamento_baixa"].append(avaliado)
        if avaliado.get("alinhamento_alta_forte"):
            listas["alinhamento_alta"].append(avaliado)
        if avaliado.get("alinhamento_baixa_forte"):
            listas["alinhamento_baixa"].append(avaliado)
        if avaliado.get("pullback_alta"):
            listas["pullback_alta"].append(avaliado)
        if avaliado.get("esticado"):
            listas["esticado"].append(avaliado)

    listas["cruzamento_alta"].sort(key=_ordenar_cruzamento)
    listas["cruzamento_baixa"].sort(key=_ordenar_cruzamento)
    listas["alinhamento_alta"].sort(key=_ordenar_alinhamento)
    listas["alinhamento_baixa"].sort(key=_ordenar_alinhamento)
    listas["pullback_alta"].sort(key=_ordenar_pullback)
    listas["esticado"].sort(key=_ordenar_esticado)

    _CACHE[chave] = (agora, listas)
    return listas
