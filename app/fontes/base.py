"""Contratos e utilidades comuns aos adaptadores de fontes."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturoTimeout
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, TypeVar

import pandas as pd

from app import config

T = TypeVar("T")


@dataclass
class Resultado:
    ok: bool
    linhas: int = 0
    avisos: list[str] = field(default_factory=list)
    erro: str | None = None


# 4 folgas: se o Yahoo pendurar, as chamadas seguintes não ficam na fila
_EXECUTOR_PRAZO = ThreadPoolExecutor(max_workers=4)


def com_prazo(fn: Callable[[], T], segundos: float | None = None) -> T:
    """Executa ``fn`` com prazo e levanta ``TimeoutError`` se estourar.

    O yfinance não aceita timeout em ``Ticker.info`` nem em ``dividends``: sem
    isso uma resposta pendurada do Yahoo trava a coleta inteira (o guard de
    coleta só libera depois de 30 min). A thread órfã fica no executor até o
    socket morrer, mas a coleta segue e registra a falha.
    """

    limite = config.TIMEOUT_S if segundos is None else segundos
    futuro = _EXECUTOR_PRAZO.submit(fn)
    try:
        return futuro.result(timeout=limite)
    except FuturoTimeout as exc:
        futuro.cancel()
        raise TimeoutError(
            f"fonte não respondeu em {limite:.0f}s"
        ) from exc


def com_retry(
    fn: Callable[[], T],
    tentativas: int = config.RETRIES,
    espera: float = 1.0,
) -> T:
    """Executa ``fn`` com backoff exponencial (1s, 2s, 4s)."""

    tentativas = max(0, int(tentativas))
    ultimo: Exception | None = None
    for retry in range(tentativas + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - o adaptador decide o que relançar
            ultimo = exc
            if retry == tentativas:
                break
            time.sleep(espera * (2**retry))
    assert ultimo is not None
    raise ultimo


def _data_normalizada(valor: Any) -> tuple[pd.Timestamp | None, str | None]:
    if valor is None:
        return None, None
    try:
        ts = pd.Timestamp(valor)
    except (TypeError, ValueError):
        return None, None
    if pd.isna(ts):
        return None, None
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize(), ts.strftime("%Y-%m-%d")


def validar_cotacoes(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Valida cotações do Yahoo.

    Descarta linhas com data inválida, data futura, ou fechamento nulo/<=0.
    Devolve o DataFrame limpo (com ``data`` em ``YYYY-MM-DD``) e os avisos.
    """

    avisos: list[str] = []
    colunas = [
        "data",
        "abertura",
        "maxima",
        "minima",
        "fechamento",
        "fechamento_ajustado",
        "volume",
    ]

    if df is None or len(df) == 0:
        return pd.DataFrame(columns=colunas), avisos

    faltantes = [c for c in ("data", "fechamento") if c not in df.columns]
    if faltantes:
        avisos.append(f"colunas ausentes: {', '.join(faltantes)}")
        return pd.DataFrame(columns=colunas), avisos

    trabalho = df.copy()
    datas: list[str | None] = []
    validas: list[bool] = []
    hoje = date.today()
    invalidas = 0
    futuras = 0

    for valor in trabalho["data"]:
        ts, texto = _data_normalizada(valor)
        if ts is None or texto is None:
            datas.append(None)
            validas.append(False)
            invalidas += 1
            continue
        if ts.date() > hoje:
            datas.append(None)
            validas.append(False)
            futuras += 1
            continue
        datas.append(texto)
        validas.append(True)

    trabalho["data"] = datas
    fechamento = pd.to_numeric(trabalho["fechamento"], errors="coerce")
    sem_fechamento = int((fechamento.isna() | (fechamento <= 0)).sum())

    mascara = pd.Series(validas, index=trabalho.index) & fechamento.notna() & (fechamento > 0)
    limpo = trabalho.loc[mascara].copy()
    limpo["fechamento"] = fechamento.loc[mascara]

    for coluna in ("abertura", "maxima", "minima", "fechamento_ajustado", "volume"):
        if coluna in limpo.columns:
            limpo[coluna] = pd.to_numeric(limpo[coluna], errors="coerce")
        else:
            limpo[coluna] = None

    limpo = limpo[colunas].reset_index(drop=True)

    if invalidas:
        avisos.append(f"{invalidas} linha(s) com data inválida descartada(s)")
    if futuras:
        avisos.append(f"{futuras} linha(s) com data futura descartada(s)")
    if sem_fechamento:
        avisos.append(f"{sem_fechamento} linha(s) com fechamento nulo/negativo descartada(s)")

    return limpo, avisos
