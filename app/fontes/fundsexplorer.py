"""Dados de FII publicados no site Funds Explorer (HTML público).

O site não expõe API: os dados do fundo vêm num JSON embutido no HTML
(``var dataLayer_content = {...}``) e são lidos com ``json.loads`` — sem
browser e sem IA. Medido em 30/out/2026: 34 FIIs em ~55 s, zero bloqueio com
uma requisição por vez a cada 1,2 s.

O preço e o histórico continuam vindo do yfinance; valor patrimonial,
cotistas e taxa continuam vindo da CVM (dado oficial). Esta fonte entra no que
o Fundamentus entrega mal para FII: DY, 52 semanas, liquidez, vacância e
número de imóveis.

Limite conhecido: a vacância só existe para FII de tijolo e alguns fundos não
publicam a série (8 dos 34 do universo em 30/out/2026).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

URL = "https://www.fundsexplorer.com.br/funds/{ticker}"
MARCADOR = "var dataLayer_content = "
INTERVALO_MIN_S = 1.2
TIMEOUT_S = 40.0
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
    """Serializa as chamadas (a coleta roda em vários workers)."""

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
    if isinstance(valor, str):
        texto = valor.strip().replace(".", "").replace(",", ".")
        if not texto:
            return None
        try:
            return float(texto)
        except ValueError:
            return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _meta_do_html(texto: str) -> dict[str, Any]:
    """Extrai ``pagePostTerms.meta`` do JSON embutido na página."""

    inicio = texto.find(MARCADOR)
    if inicio < 0:
        raise ValueError("bloco dataLayer_content não encontrado")
    dados, _ = json.JSONDecoder().raw_decode(texto[inicio + len(MARCADOR) :])
    meta = dados.get("pagePostTerms", {}).get("meta")
    if not isinstance(meta, dict) or not meta:
        raise ValueError("pagePostTerms.meta ausente")
    return meta


def _buscar(ticker: str) -> dict[str, Any]:
    def requisitar() -> dict[str, Any]:
        with httpx.Client(
            timeout=TIMEOUT_S, follow_redirects=True, headers=CABECALHOS
        ) as cliente:
            resposta = cliente.get(URL.format(ticker=ticker.lower()))
        if resposta.status_code == 404:
            raise LookupError(f"ticker sem página no Funds Explorer: {ticker}")
        resposta.raise_for_status()
        return _meta_do_html(resposta.text)

    ultimo: Exception | None = None
    for tentativa in range(TENTATIVAS + 1):
        try:
            return _com_ritmo(requisitar)
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 - a última falha é propagada
            ultimo = exc
            if tentativa < TENTATIVAS:
                time.sleep(2.0 * (tentativa + 1))
    assert ultimo is not None
    raise ultimo


def _vacancia(meta: dict[str, Any]) -> tuple[float | None, str | None]:
    """Última vacância física da série, com a data de referência."""

    series: list[tuple[int, float, str | None]] = []
    for chave, valor in meta.items():
        if not (chave.startswith("vacancia_") and chave.endswith("_vacancia_fisica")):
            continue
        numero = _numero(valor)
        if numero is None:
            continue
        try:
            indice = int(chave.split("_")[1])
        except (IndexError, ValueError):
            continue
        periodo = str(meta.get(f"vacancia_{indice}_periodo") or "")
        data = (
            f"{periodo[:4]}-{periodo[4:6]}-{periodo[6:8]}"
            if len(periodo) == 8
            else None
        )
        series.append((indice, numero, data))
    if not series:
        return None, None
    _, valor, data = max(series, key=lambda item: item[0])
    return valor, data


def papel(ticker: str, classe: str = "fii") -> tuple[dict[str, Any], list[str]]:
    """Campos canônicos de um FII no Funds Explorer.

    Devolve ``(campos, avisos)``. Só cobre FII: outras classes levantam
    ``ValueError`` (o Fundamentus continua sendo a fonte de ações/BDR).
    """

    if classe != "fii":
        raise ValueError(f"fonte não cobre {classe}")

    try:
        meta = _buscar(ticker)
    except LookupError as exc:
        return {}, [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return {}, [f"falha ao consultar Funds Explorer: {type(exc).__name__}: {exc}"]

    avisos: list[str] = []
    campos: dict[str, Any] = {}

    def guardar(campo: str, chave: str, *, positivo: bool = False) -> None:
        valor = _numero(meta.get(chave))
        if valor is None or (positivo and valor <= 0):
            return
        campos[campo] = valor

    guardar("preco", "valor", positivo=True)
    guardar("dy", "dy", positivo=True)
    guardar("max_52s", "max_52_semanas", positivo=True)
    guardar("min_52s", "min_52_semanas", positivo=True)
    guardar("liquidez_diaria_brl", "liquidezmediadiaria")

    imoveis = _numero(meta.get("assets_number"))
    if imoveis is not None and imoveis > 0:
        campos["imoveis"] = imoveis

    vacancia, data_vacancia = _vacancia(meta)
    if vacancia is not None:
        campos["vacancia"] = vacancia
        if data_vacancia:
            campos["vacancia_data_ref"] = data_vacancia

    if not campos:
        return {}, ["Funds Explorer sem campos reconhecidos para " + ticker]

    campos["dy_origem"] = "fundsexplorer"
    if "dy" not in campos:
        avisos.append("sem DY no Funds Explorer")
    return campos, avisos
