"""Mapa ticker de FII -> CNPJ usando o nome do yfinance e o nome da CVM.

Para cada ticker da classe ``fii`` do universo, compara o ``longName`` do
yfinance com ``Nome_Fundo_Classe`` do informe mensal da CVM. Só aceita
similaridade >= 0,90. Grava ``config/mapa_fii_cnpj.json`` e imprime a tabela
para conferência humana.

Uso:
    python -m scripts.mapear_fii
"""

from __future__ import annotations

import json
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import yfinance as yf

from app import config
from app.fontes import cvm
from app.fontes.yahoo import normalizar_ticker

LIMIAR = 0.90
MAPA_PATH = config.RAIZ / "config" / "mapa_fii_cnpj.json"

# a mesma normalização usada pela coleta para casar fundo novo sem mapa
normalizar_nome = cvm.normalizar_nome


def _long_name(ticker: str) -> str | None:
    try:
        info = yf.Ticker(normalizar_ticker(ticker)).info or {}
    except Exception as exc:  # noqa: BLE001 - segue para o próximo ticker
        print(f"  ! {ticker}: yfinance falhou ({type(exc).__name__}: {exc})")
        return None
    nome = info.get("longName") or info.get("shortName")
    if not nome:
        return None
    return str(nome)


def montar_mapa() -> dict[str, dict[str, Any]]:
    ano = date.today().year
    diretorio = cvm.baixar_mensal(ano)
    fundos = cvm.ler_fundos(ano, diretorio)

    nomes_cvm = [
        (cnpj, dados.get("nome"), normalizar_nome(dados.get("nome")))
        for cnpj, dados in fundos.items()
        if dados.get("nome")
    ]

    universo = config.ler_universo()
    tickers = sorted(t for t, classe in universo.items() if classe == "fii")

    mapa: dict[str, dict[str, Any]] = {}
    sem_par: list[tuple[str, str | None]] = []
    for ticker in tickers:
        nome_yahoo = _long_name(ticker)
        alvo = normalizar_nome(nome_yahoo)
        if not alvo:
            sem_par.append((ticker, nome_yahoo))
            continue

        melhor: tuple[float, str, str | None] | None = None
        for cnpj, nome_cvm, normalizado in nomes_cvm:
            if not normalizado:
                continue
            similaridade = SequenceMatcher(None, alvo, normalizado).ratio()
            if melhor is None or similaridade > melhor[0]:
                melhor = (similaridade, cnpj, nome_cvm)

        if melhor is None or melhor[0] < LIMIAR:
            sem_par.append((ticker, nome_yahoo))
            continue

        similaridade, cnpj, nome_cvm = melhor
        mapa[ticker] = {
            "cnpj": cnpj,
            "nome_cvm": nome_cvm,
            "similaridade": round(float(similaridade), 4),
        }

    MAPA_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPA_PATH.write_text(
        json.dumps(mapa, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"mapeados: {len(mapa)} de {len(tickers)} FIIs")
    print(f"{'ticker':<8} {'similaridade':>12}  nome CVM")
    for ticker in sorted(mapa):
        dados = mapa[ticker]
        print(f"{ticker:<8} {dados['similaridade']:>12.4f}  {dados['nome_cvm']}")

    if sem_par:
        print("\npara revisão (sem par >= 0,90):")
        for ticker, nome_yahoo in sem_par:
            print(f"{ticker:<8} yfinance: {nome_yahoo or '-'}")

    print(f"\nmapa gravado em {MAPA_PATH}")
    return mapa


def main() -> None:
    montar_mapa()


if __name__ == "__main__":
    main()
