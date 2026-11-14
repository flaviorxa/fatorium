"""Consenso de analistas publicado pelo MSN Money.

A página de detalhes de um ativo traz um objeto ``estimate`` embutido no HTML
com a contagem de analistas por recomendação e os preços-alvo. O mapa
``ticker -> URL`` fica em ``config/msn_map.json``. Nada é estimado: quando a
fonte não publica um campo, ele simplesmente não é gravado.
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import httpx

from app import config, db
from app.fontes.base import com_retry
from app.motor import indicadores

TIMEOUT_S = 25.0
RETRIES = 2
VALIDADE_DIAS = 180
CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

RECOMENDACOES = {
    "strongbuy": "Compra forte",
    "buy": "Compra",
    "hold": "Manter",
    "sell": "Venda",
    "underperform": "Venda forte",
}


def _ler_mapa() -> dict[str, str]:
    caminho = config.RAIZ / "config" / "msn_map.json"
    try:
        conteudo = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mapa = conteudo.get("mapa") if isinstance(conteudo, dict) else None
    if not isinstance(mapa, dict):
        return {}
    return {
        str(ticker).strip().upper(): str(url).strip()
        for ticker, url in mapa.items()
        if ticker and url
    }


def url_do_ativo(ticker: str) -> str | None:
    """Página do ativo no MSN, se mapeada."""

    return _ler_mapa().get(str(ticker or "").strip().upper())


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


def _inteiro(valor: Any) -> int | None:
    numero = _numero(valor)
    if numero is None:
        return None
    return int(round(numero))


def _data_ref(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()[:10]
    try:
        data = date.fromisoformat(texto)
    except ValueError:
        return None
    if data.year < 1900:
        return None
    return data.isoformat()


def _extrair_estimate(html: str) -> dict[str, Any] | None:
    """Lê o objeto JSON de ``estimate`` embutido no HTML."""

    marca = '"estimate"'
    posicao = html.find(marca)
    while posicao != -1:
        dois_pontos = html.find(":", posicao + len(marca))
        if dois_pontos == -1:
            return None
        inicio = html.find("{", dois_pontos + 1)
        if inicio == -1:
            return None
        try:
            valor, _ = json.JSONDecoder().raw_decode(html[inicio:])
        except json.JSONDecodeError:
            posicao = html.find(marca, posicao + 1)
            continue
        if isinstance(valor, dict):
            return valor
        posicao = html.find(marca, posicao + 1)
    return None


def _baixar_html(url: str) -> str:
    with httpx.Client(
        timeout=TIMEOUT_S, follow_redirects=True, headers=CABECALHOS
    ) as cliente:
        resposta = cliente.get(url)
        resposta.raise_for_status()
        return resposta.text


def upside_pct(alvo: Any, preco: Any) -> float | None:
    """Upside até o alvo: ``(alvo / preço - 1) * 100``.

    Fica ``None`` quando ``alvo/preço`` sai de ``[0,5; 2,5]``: nesse caso o
    alvo provavelmente é de outra classe do papel (unit/ON/PN).
    """

    return indicadores.avaliar_alvo(alvo, preco)[0]


def alvo_classe_duvidosa(alvo: Any, preco: Any) -> bool:
    """Indica se o alvo do MSN parece ser de outra classe do papel."""

    return indicadores.avaliar_alvo(alvo, preco)[1]


def consenso(ticker: str) -> tuple[dict[str, Any], list[str]]:
    """Consenso de analistas do MSN para ``ticker``.

    Devolve ``(campos, avisos)``. Em falha de mapeamento/rede devolve campos
    vazios, registra a falha e nunca levanta exceção.
    """

    ticker = str(ticker or "").strip().upper()
    url = _ler_mapa().get(ticker)
    if not url:
        motivo = "ticker sem página no MSN"
        try:
            db.registrar_falha("msn", ticker, "mapeamento", motivo)
        except Exception:  # pragma: no cover - o log não pode derrubar a coleta
            pass
        return {}, [motivo]

    try:
        html = com_retry(lambda: _baixar_html(url), tentativas=RETRIES)
    except Exception as exc:  # noqa: BLE001
        motivo = f"{type(exc).__name__}: {exc}"
        try:
            db.registrar_falha("msn", ticker, "consenso", motivo)
        except Exception:  # pragma: no cover
            pass
        return {}, [f"falha ao consultar MSN: {motivo}"]

    estimate = _extrair_estimate(html)
    if not isinstance(estimate, dict):
        return {}, ["sem consenso na fonte"]

    avisos: list[str] = []
    campos: dict[str, Any] = {}

    total = _inteiro(estimate.get("numberOfAnalysts"))
    if total is not None:
        campos["consenso_total"] = total

    contagem = estimate.get("analystRecommendation")
    if isinstance(contagem, dict):
        compra = sum(
            valor
            for valor in (
                _inteiro(contagem.get("strongBuy")),
                _inteiro(contagem.get("buy")),
            )
            if valor is not None
        )
        neutro = _inteiro(contagem.get("hold"))
        venda = sum(
            valor
            for valor in (
                _inteiro(contagem.get("sell")),
                _inteiro(contagem.get("underperform")),
            )
            if valor is not None
        )
        campos["consenso_compra"] = compra
        campos["consenso_neutro"] = neutro or 0
        campos["consenso_venda"] = venda
        if total is None:
            total = compra + (neutro or 0) + venda
            campos["consenso_total"] = total

    taxa = _numero(estimate.get("recommendationRate"))
    if taxa is not None:
        campos["taxa_recomendacao"] = taxa

    recomendacao = estimate.get("recommendation")
    if recomendacao is not None:
        texto = RECOMENDACOES.get(str(recomendacao).strip().lower())
        if texto is not None:
            campos["recomendacao"] = texto

    for origem, destino in (
        ("meanPriceTarget", "alvo_medio"),
        ("medianPriceTarget", "alvo_mediana"),
        ("highPriceTarget", "alvo_max"),
        ("lowPriceTarget", "alvo_min"),
    ):
        valor = _numero(estimate.get(origem))
        if valor is not None:
            campos[destino] = valor

    data_ref = _data_ref(estimate.get("dateLastUpdated"))
    if data_ref is not None:
        campos["data_ref"] = data_ref

    if not campos:
        return {}, ["sem consenso na fonte"]

    if not isinstance(contagem, dict) and len(campos) > (1 if data_ref else 0):
        avisos.append("sem contagem por analista na fonte")

    if "consenso_total" in campos and int(campos["consenso_total"]) < 2:
        quantidade = int(campos["consenso_total"])
        rotulo = "1 analista" if quantidade == 1 else f"{quantidade} analistas"
        avisos.append(f"consenso com {rotulo}: não é consenso")

    if data_ref is not None:
        idade = (date.today() - date.fromisoformat(data_ref)).days
        if idade > VALIDADE_DIAS:
            avisos.append(f"consenso desatualizado desde {data_ref}")

    return campos, avisos
