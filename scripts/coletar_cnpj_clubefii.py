"""Mapeia todos os CNPJs de FIIs a partir da lista do ClubeFII.

O site do ClubeFII devolve 403 para acesso direto; a lista é lida pelo
extrator ``r.jina.ai``, que devolve o conteúdo em markdown. O parser aceita
apenas linhas no formato::

    | [003H11](https://www.clubefii.com.br/fiis/003H11) | [30.816.679/0001-52](https://...) |

Uso:
    python -m scripts.coletar_cnpj_clubefii
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import httpx

from app import config

URL = "https://www.clubefii.com.br/fundos-imobiliarios/67341/lista-cnpj"
URL_EXTRATOR = f"https://r.jina.ai/{URL}"
TIMEOUT_S = 90.0
MINIMO_CNPJ = 100

CNPJ_PATH = config.RAIZ / "config" / "cnpj_clubefii.json"
MAPA_PATH = config.RAIZ / "config" / "mapa_fii_cnpj.json"

_LINHA_RE = re.compile(
    r"\[\s*([A-Z0-9]{4,7}1[12]?)\s*\]\([^)]*\)"
    r"\s*\|\s*"
    r"\[\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\s*\]"
)


def extrair_pares(texto: str) -> list[tuple[str, str]]:
    """Devolve os pares ``(ticker, CNPJ)`` na ordem em que aparecem."""

    pares: list[tuple[str, str]] = []
    for linha in texto.splitlines():
        encontrado = _LINHA_RE.search(linha)
        if encontrado is not None:
            pares.append((encontrado.group(1), encontrado.group(2)))
    return pares


def validar_pares(pares: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Aborta se a resposta veio vazia/parcial, sem tocar nos arquivos."""

    lista = list(pares)
    if len(lista) < MINIMO_CNPJ:
        raise RuntimeError(
            f"lista do ClubeFII veio com {len(lista)} CNPJs "
            f"(mínimo {MINIMO_CNPJ}); arquivos não foram alterados"
        )
    return lista


def baixar(url: str = URL_EXTRATOR, timeout: float = TIMEOUT_S) -> str:
    """Baixa a lista pelo extrator, com uma nova tentativa em falha de rede."""

    ultimo: Exception | None = None
    for tentativa in range(2):
        try:
            with httpx.Client(
                timeout=timeout, follow_redirects=True, max_redirects=5
            ) as cliente:
                resposta = cliente.get(url)
                resposta.raise_for_status()
                return resposta.text
        except Exception as exc:  # noqa: BLE001 - a última falha é propagada
            ultimo = exc
            if tentativa == 0:
                time.sleep(1.0)
    assert ultimo is not None
    raise RuntimeError(
        f"não foi possível baixar a lista do ClubeFII: "
        f"{type(ultimo).__name__}: {ultimo}"
    ) from ultimo


def ler_mapa(caminho: Path = MAPA_PATH) -> dict[str, dict[str, Any]]:
    if not caminho.exists():
        return {}
    try:
        conteudo = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return conteudo if isinstance(conteudo, dict) else {}


def mesclar(
    existente: dict[str, dict[str, Any]],
    cnpjs: dict[str, str],
    tickers_fii: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Funde os CNPJs do ClubeFII sem perder o que já estava mapeado."""

    mapa: dict[str, dict[str, Any]] = dict(existente)
    for ticker in tickers_fii:
        cnpj = cnpjs.get(ticker)
        if not cnpj:
            continue
        atual = mapa.get(ticker)
        if isinstance(atual, dict):
            novo = dict(atual)
            novo["cnpj"] = cnpj
            mapa[ticker] = novo
        else:
            mapa[ticker] = {"cnpj": cnpj, "fonte": "clubefii"}
    return mapa


def _tickers_fii(universo: dict[str, str]) -> list[str]:
    return sorted(ticker for ticker, classe in universo.items() if classe == "fii")


def _gravar_json(caminho: Path, conteudo: Any) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(conteudo, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def processar(
    texto: str,
    *,
    cnpj_path: Path = CNPJ_PATH,
    mapa_path: Path = MAPA_PATH,
    universo: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Valida a resposta, grava a lista completa e funde o mapa do coletor."""

    pares = validar_pares(extrair_pares(texto))
    if universo is None:
        universo = config.ler_universo()

    cnpjs = dict(pares)
    _gravar_json(
        cnpj_path,
        {
            "atualizado_em": date.today().isoformat(),
            "fonte": URL_EXTRATOR,
            "cnpjs": cnpjs,
        },
    )

    tickers_fii = _tickers_fii(universo)
    mapa = mesclar(ler_mapa(mapa_path), cnpjs, tickers_fii)
    _gravar_json(mapa_path, mapa)

    mapeados = [t for t in tickers_fii if t in cnpjs]
    sem_cnpj = [t for t in tickers_fii if t not in cnpjs]

    print(f"CNPJs no arquivo do ClubeFII: {len(cnpjs)}")
    print(
        f"FIIs do universo mapeados: {len(mapeados)} de {len(tickers_fii)}"
    )
    if sem_cnpj:
        print("continuam sem CNPJ: " + ", ".join(sem_cnpj))
    else:
        print("todos os FIIs do universo têm CNPJ")

    return {
        "total_clubefii": len(cnpjs),
        "mapeados": mapeados,
        "sem_cnpj": sem_cnpj,
    }


def main() -> None:
    texto = baixar()
    processar(texto)


if __name__ == "__main__":
    main()
