"""Configuração central da plataforma de investimentos."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

HOST = os.getenv("INVEST_HOST", "127.0.0.1")
PORTA = int(os.getenv("INVEST_PORTA", "8100"))

RAIZ = Path(__file__).resolve().parent.parent
DB_PATH = RAIZ / "data" / "invest.db"
LISTA_ATIVOS = RAIZ / "config" / "universo.md"
MAPA_FII_CNPJ = RAIZ / "config" / "mapa_fii_cnpj.json"

MAX_CONCORRENCIA = 3
TIMEOUT_S = 30
RETRIES = 3
PERIODO_PADRAO = "2y"

BENCHMARKS = {
    "acao_b3": "^BVSP",
    "fii": "^BVSP",
    "etf": "^BVSP",
    "bdr": "^GSPC",
    "cripto": "^GSPC",
    "indice": "^BVSP",
}

# Chave da seção no markdown -> classe interna.
MAPA_CLASSE = {
    "Criptomoedas": "cripto",
    "Ações": "acao_b3",
    "BDRs": "bdr",
    "ETFs": "etf",
    "Fundos Imobiliários e Fi-Infras": "fii",
}

_TICKER_RE = re.compile(r"\b[A-Z][A-Z0-9]{3}\d{1,2}\b")
_SECAO_RE = re.compile(r"^##\s+(.+?)\s*(?:\(\d+\))?\s*$")
_CRIPTO_RE = re.compile(r"^[A-Z]{2,10}(?:-(?:BRL|USD))?$")


def _normalizar_secao(titulo: str) -> str:
    return titulo.strip()


def ler_universo(caminho: Path | str | None = None) -> dict[str, str]:
    """Lê a lista de ativos e devolve ``{ticker: classe}``.

    A seção "Criptomoedas" aceita símbolos como ``BTC`` e os converte para o
    par em reais da exchange (``BTC-BRL``). As demais seções usam o padrão de
    ticker da B3 (quatro caracteres alfanuméricos seguidos de um ou dois
    dígitos).
    """

    arquivo = (
        Path(caminho)
        if caminho is not None
        else Path(os.getenv("INVEST_LISTA_ATIVOS") or LISTA_ATIVOS)
    )
    if not arquivo.exists():
        raise FileNotFoundError(
            f"Lista de ativos não encontrada em {arquivo}. "
            "Ajuste LISTA_ATIVOS em app/config.py ou crie o arquivo."
        )

    universo: dict[str, str] = {}
    secao_atual: str | None = None

    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if linha.strip() == "---":
            secao_atual = None
            continue
        m = _SECAO_RE.match(linha)
        if m:
            secao_atual = _normalizar_secao(m.group(1))
            continue
        if secao_atual is None or not linha.strip() or linha.lstrip().startswith(">"):
            continue

        classe = MAPA_CLASSE.get(secao_atual)
        if classe is None:
            continue

        conteudo = linha.lstrip("-* ").strip()
        if classe == "cripto":
            for parte in conteudo.split(","):
                simbolo = parte.strip().lstrip("-* ").strip()
                simbolo = simbolo.split("—")[0].split(" - ")[0].strip()
                for token in re.split(r"[;\s]+", simbolo):
                    token = token.strip().upper()
                    if not token or not _CRIPTO_RE.match(token):
                        continue
                    ticker = token if token.endswith("-BRL") else f"{token.split('-')[0]}-BRL"
                    universo[ticker] = classe
        else:
            for ticker in _TICKER_RE.findall(conteudo.upper()):
                universo[ticker] = classe

    for benchmark in set(BENCHMARKS.values()):
        universo.setdefault(benchmark, "indice")

    return universo


def caminho_mapa_fii() -> Path:
    """Caminho do mapa ticker–CNPJ dos FIIs (``INVEST_MAPA_FII`` opcional)."""

    return Path(os.getenv("INVEST_MAPA_FII") or MAPA_FII_CNPJ)


def ler_mapa_fii() -> dict[str, Any]:
    """Lê o mapa ticker–CNPJ dos FIIs; arquivo ausente ou inválido vira ``{}``."""

    try:
        conteudo = json.loads(caminho_mapa_fii().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return conteudo if isinstance(conteudo, dict) else {}
