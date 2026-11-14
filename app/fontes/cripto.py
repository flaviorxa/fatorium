"""Cotações de criptomoedas em reais (OKX, API v5 pública).

O Yahoo só publica pares em dólar (``BTC-USD``), mas o site exibe tudo em R$.
Coletar o par em dólar e imprimir ``R$`` deixava o Bitcoin ~5x abaixo do real.
A OKX negocia ``BTC-BRL`` com livro em reais e devolve candles diários UTC e o
spot, sem chave e sem cadastro. O Mercado Bitcoin (API v4) foi a fonte anterior
e passou a responder 403 à VM.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import requests

from app import config, db
from app.fontes.base import Resultado, com_prazo, com_retry, validar_cotacoes

BASE = "https://www.okx.com/api/v5"
_CABECALHOS = {"User-Agent": "invest/1.0 (analise pessoal)"}
_DIAS = {
    "1mo": 31,
    "3mo": 92,
    "6mo": 183,
    "1y": 366,
    "2y": 731,
    "5y": 1827,
    "10y": 3653,
    "max": 3653,
}
_COLUNAS = [
    "data",
    "abertura",
    "maxima",
    "minima",
    "fechamento",
    "fechamento_ajustado",
    "volume",
]
# A OKX devolve no máximo 100 candles por requisição (spot).
_PAGINA = 100
_MAX_PAGINAS = 40


def par(ticker: str) -> str:
    """``BTC`` ou o legado ``BTC-USD`` -> ``BTC-BRL``."""

    simbolo = (ticker or "").strip().upper()
    return f"{simbolo.split('-')[0]}-BRL"


def _get(caminho: str, **params: Any) -> list[Any]:
    resposta = requests.get(
        f"{BASE}/{caminho}",
        params=params,
        headers=_CABECALHOS,
        timeout=config.TIMEOUT_S,
    )
    resposta.raise_for_status()
    dados = resposta.json()
    if str(dados.get("code")) != "0":
        raise RuntimeError(f"OKX: {dados.get('msg')} (código {dados.get('code')})")
    return dados.get("data") or []


def _falha(ticker: str, etapa: str, exc: BaseException) -> Resultado:
    mensagem = f"{type(exc).__name__}: {exc}"
    try:
        db.registrar_falha("okx", ticker, etapa, mensagem)
    except Exception:  # pragma: no cover - nunca deixar o log derrubar a coleta
        pass
    return Resultado(ok=False, linhas=0, avisos=[], erro=mensagem)


def _candles(simbolo: str, dias: int) -> list[list[str]]:
    """Candles diários UTC, do mais antigo para o mais novo, sem duplicata."""

    linhas = _get(
        "market/candles",
        instId=simbolo,
        bar="1Dutc",
        limit=min(max(int(dias), 1), _PAGINA),
    )
    vistos = {linha[0] for linha in linhas}
    paginas = 0
    while linhas and len(linhas) < dias and paginas < _MAX_PAGINAS:
        paginas += 1
        novas = _get(
            "market/history-candles",
            instId=simbolo,
            bar="1Dutc",
            after=linhas[-1][0],
            limit=_PAGINA,
        )
        novas = [linha for linha in novas if linha[0] not in vistos]
        if not novas:
            break
        linhas.extend(novas)
        vistos.update(linha[0] for linha in novas)
    return linhas[:dias][::-1]


def _spot(simbolo: str) -> tuple[float, str] | None:
    dados = _get("market/ticker", instId=simbolo)
    if not dados:
        return None
    item = dados[0]
    valor = float(item["last"])
    quando = dt.datetime.fromtimestamp(
        int(item["ts"]) / 1000, dt.timezone.utc
    )
    return valor, quando.date().isoformat()


def historico(
    ticker: str, periodo: str = config.PERIODO_PADRAO
) -> pd.DataFrame | Resultado:
    """Histórico diário em BRL, já validado (mesmo contrato de ``yahoo.historico``)."""

    simbolo = par(ticker)
    dias = _DIAS.get(periodo, 731)

    try:
        linhas = com_retry(lambda: com_prazo(lambda: _candles(simbolo, dias)))
    except Exception as exc:  # noqa: BLE001
        return _falha(ticker, "historico", exc)

    if not linhas:
        return _falha(ticker, "historico", RuntimeError("OKX não devolveu cotações"))

    registros: list[dict[str, Any]] = []
    for linha in linhas:
        timestamp, abertura, maxima, minima, fechamento, volume = linha[:6]
        registros.append(
            {
                "data": dt.datetime.fromtimestamp(
                    int(timestamp) / 1000, dt.timezone.utc
                ).date().isoformat(),
                "abertura": abertura,
                "maxima": maxima,
                "minima": minima,
                "fechamento": fechamento,
                "fechamento_ajustado": fechamento,
                "volume": volume,
            }
        )

    # O candle do dia corrente já vem da OKX (barra aberta); o spot cobre o
    # caso de o candle atrasar.
    try:
        spot = com_prazo(lambda: _spot(simbolo))
    except Exception:  # noqa: BLE001 - spot é opcional, o candle já basta
        spot = None
    if spot and spot[1] > registros[-1]["data"]:
        registros.append(
            {
                "data": spot[1],
                "abertura": spot[0],
                "maxima": spot[0],
                "minima": spot[0],
                "fechamento": spot[0],
                "fechamento_ajustado": spot[0],
                "volume": None,
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
