"""Preço-alvo e recomendação do Research BTG Pactual.

A página pública ``/research/ativo/<TICKER>`` é renderizada no servidor e traz
o card do ativo: preço, recomendação e preço-alvo. O próprio HTML carrega o
objeto ``recommendation`` no estado da aplicação::

    "recommendation":{"targetPrice":"90.00000000000","recommendation":"COMPRA",
                      "recommendationDate":"2026-10-30T09:29:16.489Z", ...}

É esse objeto que vale: o texto renderizado existe para leitura humana, o JSON
é o dado que o site usa. Sem cobertura, o objeto não aparece.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import date
from typing import Any

import httpx

from app import db
from app.fontes.base import com_prazo, com_retry

URL = "https://content.btgpactual.com/research/ativo/{ticker}"
TIMEOUT_S = 45.0
INTERVALO_MIN_S = 0.6
CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
RECOMENDACOES = {"COMPRA": "Compra", "NEUTRO": "Neutro", "VENDA": "Venda"}

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


def _numero(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    return numero


def _data(valor: Any) -> str | None:
    if not valor:
        return None
    texto = str(valor).strip()[:10]
    try:
        return date.fromisoformat(texto).isoformat()
    except ValueError:
        return None


def extrair(html: str) -> tuple[dict[str, Any], list[str]]:
    """Lê o objeto ``recommendation`` do HTML (função pura)."""

    marca = '"recommendation":{'
    posicao = html.find(marca)
    if posicao == -1:
        return {}, ["sem cobertura no BTG"]

    inicio = html.find("{", posicao)
    try:
        objeto, _ = json.JSONDecoder().raw_decode(html[inicio:])
    except json.JSONDecodeError:
        return {}, ["recommendation ilegível no HTML do BTG"]
    if not isinstance(objeto, dict):
        return {}, ["recommendation ilegível no HTML do BTG"]

    alvo = _numero(objeto.get("targetPrice"))
    if alvo is None or alvo <= 0:
        return {}, ["sem preço-alvo no BTG"]

    campos: dict[str, Any] = {"btg_alvo": alvo}

    avaliacao = str(objeto.get("recommendation") or "").strip().upper()
    if avaliacao in RECOMENDACOES:
        campos["btg_recomendacao"] = RECOMENDACOES[avaliacao]

    data_alvo = _data(objeto.get("recommendationDate"))
    if data_alvo:
        campos["btg_data"] = data_alvo

    return campos, []


def _baixar(ticker: str) -> str | None:
    url = URL.format(ticker=ticker)
    with httpx.Client(
        timeout=TIMEOUT_S, follow_redirects=True, headers=CABECALHOS
    ) as cliente:
        resposta = cliente.get(url)
        if resposta.status_code in {404, 410}:
            return None
        resposta.raise_for_status()
        return resposta.text


def preco_alvo(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Campos do card do BTG e avisos; nunca levanta exceção."""

    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return {}, ["ticker vazio"]

    try:
        html = com_prazo(
            lambda: com_retry(lambda: _com_ritmo(lambda: _baixar(ticker)), tentativas=1),
            segundos=TIMEOUT_S + 20,
        )
    except Exception as exc:  # noqa: BLE001 - a coleta não pode parar aqui
        motivo = f"{type(exc).__name__}: {exc}"
        try:
            db.registrar_falha("btg", ticker, "preco_alvo", motivo)
        except Exception:  # pragma: no cover
            pass
        return {}, [f"falha ao consultar BTG: {motivo}"]

    if html is None:
        return {}, ["sem página no BTG para este ticker"]

    return extrair(html)
