"""Adaptador Yahoo Finance (yfinance)."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
import yfinance as yf

from app import config, db
from app.fontes.base import Resultado, com_prazo, com_retry, validar_cotacoes

_TICKER_B3 = re.compile(r"^[A-Z][A-Z0-9]{3}\d{1,2}$")
_COLUNAS = [
    "data",
    "abertura",
    "maxima",
    "minima",
    "fechamento",
    "fechamento_ajustado",
    "volume",
]


def normalizar_ticker(ticker: str) -> str:
    """Adiciona ``.SA`` aos tickers da B3 e mantém símbolos globais intactos."""

    t = (ticker or "").strip()
    if any(marca in t for marca in ("^", "=", "-USD")):
        return t
    if _TICKER_B3.match(t):
        return f"{t}.SA"
    return t


def _falha(ticker: str, etapa: str, exc: BaseException) -> Resultado:
    mensagem = f"{type(exc).__name__}: {exc}"
    try:
        db.registrar_falha("yahoo", ticker, etapa, mensagem)
    except Exception:  # pragma: no cover - nunca deixar o log derrubar a coleta
        pass
    return Resultado(ok=False, linhas=0, avisos=[], erro=mensagem)


def _data_texto(indice: Any) -> str:
    ts = pd.Timestamp(indice)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.strftime("%Y-%m-%d")


def historico(ticker: str, periodo: str = config.PERIODO_PADRAO) -> pd.DataFrame | Resultado:
    """Histórico diário de cotações, já validado.

    Em sucesso devolve ``DataFrame``; em falha devolve ``Resultado(ok=False)``
    e registra a falha, sem propagar exceção.
    """

    simbolo = normalizar_ticker(ticker)

    def baixar() -> pd.DataFrame:
        return yf.Ticker(simbolo).history(
            period=periodo,
            auto_adjust=False,
            timeout=config.TIMEOUT_S,
        )

    try:
        bruto = com_retry(baixar)
    except Exception as exc:  # noqa: BLE001
        return _falha(ticker, "historico", exc)

    if bruto is None or len(bruto) == 0:
        return _falha(ticker, "historico", RuntimeError("Yahoo não devolveu cotações"))

    registros: list[dict[str, Any]] = []
    for indice, linha in bruto.iterrows():
        registros.append(
            {
                "data": _data_texto(indice),
                "abertura": linha.get("Open"),
                "maxima": linha.get("High"),
                "minima": linha.get("Low"),
                "fechamento": linha.get("Close"),
                "fechamento_ajustado": linha.get("Adj Close"),
                "volume": linha.get("Volume"),
            }
        )

    df = pd.DataFrame(registros, columns=_COLUNAS)
    limpo, avisos = validar_cotacoes(df)
    if limpo.empty:
        return Resultado(
            ok=False,
            linhas=0,
            avisos=avisos,
            erro="nenhuma cotação válida após validação",
        )

    limpo.attrs["avisos"] = avisos
    return limpo


def fundamentos_basicos(ticker: str) -> dict[str, Any]:
    """Dados básicos de ``Ticker(t).info``, tolerantes a falha.

    Lê o ``info`` uma única vez e devolve apenas o que interessa ao motor:
    ``price_to_book`` (razão P/VP), ``book_value``, ``market_cap``,
    ``nome_longo`` e ``moeda``. Qualquer campo ausente ou falha de rede vira
    ``None``; a função nunca levanta exceção.
    """

    vazio: dict[str, Any] = {
        "price_to_book": None,
        "book_value": None,
        "market_cap": None,
        "nome_longo": None,
        "moeda": None,
    }
    simbolo = normalizar_ticker(ticker)
    try:
        info = com_prazo(lambda: yf.Ticker(simbolo).info)
    except Exception:  # noqa: BLE001 - fonte opcional nunca derruba a coleta
        return vazio
    if not isinstance(info, dict):
        return vazio

    def numero(chave: str) -> float | None:
        valor = info.get(chave)
        if valor is None or isinstance(valor, bool):
            return None
        try:
            convertido = float(valor)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(convertido) else convertido

    def texto(chave: str) -> str | None:
        valor = info.get(chave)
        if valor is None:
            return None
        convertido = str(valor).strip()
        return convertido or None

    return {
        "price_to_book": numero("priceToBook"),
        "book_value": numero("bookValue"),
        "market_cap": numero("marketCap"),
        "nome_longo": texto("longName"),
        "moeda": texto("currency"),
    }


def proventos(ticker: str) -> pd.DataFrame | Resultado:
    """Proventos (``data_ex``, ``valor``) a partir de ``Ticker.dividends``."""

    simbolo = normalizar_ticker(ticker)

    def baixar() -> pd.Series:
        return yf.Ticker(simbolo).dividends

    try:
        serie = com_retry(lambda: com_prazo(baixar))
    except Exception as exc:  # noqa: BLE001
        return _falha(ticker, "proventos", exc)

    if serie is None or len(serie) == 0:
        return pd.DataFrame(columns=["data_ex", "valor"])

    registros: list[dict[str, Any]] = []
    for indice, valor in serie.items():
        try:
            numero = float(valor)
        except (TypeError, ValueError):
            continue
        if pd.isna(numero) or numero <= 0:
            continue
        registros.append(
            {
                "data_ex": _data_texto(indice),
                "valor": numero,
            }
        )

    return pd.DataFrame(registros, columns=["data_ex", "valor"])
