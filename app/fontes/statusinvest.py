"""Dados de ETF do Status Invest (HTML público, sem API).

O site entrega os dados no próprio HTML (renderizado no servidor): patrimônio
líquido, número de cotistas, liquidez média diária, CNPJ e taxa de
administração. Medido em 30/out/2026 nos 16 ETFs do universo: 16/16 com 200,
0,2–0,4 s por ticker, sem bloqueio; patrimônio e cotistas em 15/16 (GPCA11
ainda não publica), taxa em 10/16 — nos demais o próprio site mostra "–".

Preço e histórico continuam vindo do yfinance.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

URL = "https://statusinvest.com.br/etfs/{ticker}"
INTERVALO_MIN_S = 1.2
TIMEOUT_S = 45.0
TENTATIVAS = 2
CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    )
}

_LOCK = threading.Lock()
_ULTIMA_REQUISICAO = 0.0


def _com_ritmo(fn):
    global _ULTIMA_REQUISICAO
    with _LOCK:
        espera = INTERVALO_MIN_S - (time.monotonic() - _ULTIMA_REQUISICAO)
        if espera > 0:
            time.sleep(espera)
        try:
            return fn()
        finally:
            _ULTIMA_REQUISICAO = time.monotonic()


def _numero(texto: str | None) -> float | None:
    if not texto:
        return None
    bruto = texto.strip().replace(".", "").replace(",", ".")
    try:
        return float(bruto)
    except ValueError:
        return None


def _texto(html: str) -> str:
    """HTML → texto numa linha, para casar rótulo e valor."""

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&nbsp;", " ")


def _depois(texto: str, rotulo: str) -> float | None:
    i = texto.find(rotulo)
    if i < 0:
        return None
    # o valor tem de vir logo depois do rótulo: em fundo que ainda não
    # publica, o site escreve "--" e o número seguinte é de outro campo
    achado = re.match(
        r"\s*([\d]{1,3}(?:\.[\d]{3})+(?:,\d{2})?)",
        texto[i + len(rotulo) : i + len(rotulo) + 80],
    )
    return _numero(achado.group(1)) if achado else None


def extrair(html: str) -> dict[str, Any]:
    """Campos do ETF a partir do HTML da página (função pura)."""

    texto = _texto(html)
    campos: dict[str, Any] = {}

    patrimonio = _depois(texto, "Patrimônio líquido R$")
    if patrimonio is not None:
        campos["patrimonio_liquido"] = patrimonio

    cotistas = _depois(texto, "Nº de cotistas")
    if cotistas is not None:
        campos["cotistas"] = cotistas

    liquidez = _depois(texto, "Liquidez média diária R$")
    if liquidez is not None:
        campos["liquidez_diaria_brl"] = liquidez

    # "Taxa de adm. administração 0,54 % a.a" (rótulo quebrado em dois spans);
    # quando o fundo não informa, o site mostra "-" e nada é extraído.
    i = texto.find("Taxa de adm.")
    if i >= 0:
        trecho = texto[i : i + 220]
        taxa = re.search(r"([\d,]+) ?% ?a\.a", trecho)
        if taxa:
            campos["taxa_administracao_etf"] = _numero(taxa.group(1))

    cnpj = re.search(r"CNPJ\s*([\d]{2}\.[\d]{3}\.[\d]{3}/[\d]{4}-[\d]{2})", texto)
    if cnpj:
        campos["cnpj"] = cnpj.group(1)

    return campos


def _buscar(ticker: str) -> str:
    # urllib (stdlib) e não httpx: o Cloudflare do site bloqueia o TLS do
    # httpx neste interpretador (403), enquanto o urllib passa (200) — medido
    # em 30/out/2026, mesmo IP, mesmo User-Agent.
    def requisitar() -> str:
        pedido = urllib.request.Request(
            URL.format(ticker=ticker.lower()), headers=CABECALHOS
        )
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT_S) as resposta:
                return resposta.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(
                    f"ticker sem página no Status Invest: {ticker}"
                ) from exc
            raise

    ultimo: Exception | None = None
    for tentativa in range(TENTATIVAS + 1):
        try:
            return _com_ritmo(requisitar)
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            if tentativa < TENTATIVAS:
                time.sleep(2.0 * (tentativa + 1))
    assert ultimo is not None
    raise ultimo


def papel(ticker: str, classe: str = "etf") -> tuple[dict[str, Any], list[str]]:
    """Campos canônicos de um ETF. Só cobre ETF."""

    if classe != "etf":
        raise ValueError(f"fonte não cobre {classe}")

    try:
        campos = extrair(_buscar(ticker))
    except LookupError as exc:
        return {}, [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return {}, [f"falha ao consultar Status Invest: {type(exc).__name__}: {exc}"]

    if not campos:
        return {}, ["Status Invest sem campos reconhecidos para " + ticker]
    avisos = [] if "taxa_administracao_etf" in campos else ["sem taxa de administração na fonte"]
    return campos, avisos
