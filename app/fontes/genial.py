"""Preço-alvo publicado no Genial Analisa (card da página do ativo).

A página é renderizada no servidor, mas o site recusa a conexão da VM
(ERR_HTTP2_PROTOCOL_ERROR/ timeouts). O texto é lido pelo leitor público
``r.jina.ai``, que devolve o markdown já com o card::

    Manter [VALE3] Preço (30/10) R$62,02 Preço Alvo R$86,00 Potencial +38,66%

Sem cobertura, o card mostra ``Sem Cobertura`` no lugar do alvo. Nada é
estimado: campo ausente não é gravado.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import date
from typing import Any

import httpx

from app import db
from app.fontes.base import com_prazo, com_retry

BASE = "https://analisa.genialinvestimentos.com.br/acoes/{ticker}/"
LEITOR = "https://r.jina.ai/"
TIMEOUT_S = 100.0
INTERVALO_MIN_S = 3.0
CABECALHOS = {"Accept": "text/plain"}

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
    bruto = texto.strip()
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        numero = float(bruto)
    except ValueError:
        return None
    if not math.isfinite(numero):
        return None
    return numero


def _data(preco_data: str | None) -> str | None:
    """``30/10`` ou ``30/10/2026`` → ISO, inferindo o ano quando falta."""

    if not preco_data:
        return None
    partes = preco_data.strip().split("/")
    if len(partes) not in (2, 3):
        return None
    try:
        dia, mes = int(partes[0]), int(partes[1])
        if len(partes) == 3:
            return date(int(partes[2]), mes, dia).isoformat()
        hoje = date.today()
        try:
            return date(hoje.year, mes, dia).isoformat()
        except ValueError:
            return None
    except ValueError:
        return None


def _normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


def extrair(texto: str, ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Lê o card do ativo no texto devolvido pelo leitor (função pura)."""

    ticker = str(ticker).strip().upper()
    limpo = _normalizar(texto)
    if not limpo:
        return {}, ["página vazia na Genial"]

    if "Sem Cobertura" in limpo:
        return {}, ["sem cobertura na Genial"]

    achado = re.search(
        r"Preço\s*\((\d{2}/\d{2}(?:/\d{4})?)\)\s*R\$\s*([\d.,]+)"
        r"\s*Preço\s+Alvo\s*(?:R\$\s*([\d.,]+))?",
        limpo,
    )
    if achado is None:
        return {}, ["preço-alvo não encontrado na página da Genial"]

    alvo = _numero(achado.group(3))
    if alvo is None or alvo <= 0:
        return {}, ["sem preço-alvo na página da Genial"]

    campos: dict[str, Any] = {"genial_alvo": alvo}

    preco = _numero(achado.group(2))
    if preco is not None and preco > 0:
        campos["genial_preco"] = preco
        data_preco = _data(achado.group(1))
        if data_preco:
            campos["genial_preco_data"] = data_preco

    recomendacao = re.search(
        rf"(Comprar|Compra|Manter|Vender|Venda|Neutro)\s*\[\s*{re.escape(ticker)}\s*\]",
        limpo,
    )
    if recomendacao:
        campos["genial_recomendacao"] = recomendacao.group(1)

    potencial = re.search(r"Potencial\s*([+-]?[\d.,]+)\s*%", limpo)
    if potencial:
        valor = _numero(potencial.group(1))
        if valor is not None:
            campos["genial_potencial"] = valor

    return campos, []


def _baixar(ticker: str) -> str | None:
    url = LEITOR + BASE.format(ticker=ticker)
    with httpx.Client(
        timeout=TIMEOUT_S, follow_redirects=True, headers=CABECALHOS
    ) as cliente:
        resposta = cliente.get(url)
        if resposta.status_code in {404, 410, 422}:
            # ticker sem página/cobertura: não é falha e não vale retentar
            return None
        resposta.raise_for_status()
        return resposta.text


def preco_alvo(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Campos do card da Genial e avisos; nunca levanta exceção."""

    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return {}, ["ticker vazio"]

    try:
        texto = com_prazo(
            lambda: com_retry(lambda: _com_ritmo(lambda: _baixar(ticker)), tentativas=1),
            segundos=TIMEOUT_S + 20,
        )
    except Exception as exc:  # noqa: BLE001 - a coleta não pode parar aqui
        motivo = f"{type(exc).__name__}: {exc}"
        try:
            db.registrar_falha("genial", ticker, "preco_alvo", motivo)
        except Exception:  # pragma: no cover
            pass
        return {}, [f"falha ao consultar Genial: {motivo}"]

    if texto is None:
        return {}, ["sem cobertura na Genial"]

    campos, avisos = extrair(texto, ticker)
    return campos, avisos
