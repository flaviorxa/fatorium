"""Stock Guide do BTG em Excel, enviado à mão pela tela /fontes.

O layout do guia muda de mês para mês, então o parser não fixa coluna nem
aba: procura, em todas as abas, a linha de cabeçalho que tenha uma coluna de
ticker e uma de preço-alvo, e lê as linhas abaixo. Número em formato brasileiro
("R$ 90,00") e valor numérico são aceitos.
"""

from __future__ import annotations

import io
import math
import re
from datetime import date
from typing import Any

from openpyxl import load_workbook

from app import db

FONTE = "btg_planilha"
COLUNAS_TICKER = ("ticker", "papel", "codigo", "código", "ativo")
COLUNAS_ALVO = (
    "preço-alvo",
    "preco-alvo",
    "preço alvo",
    "preco alvo",
    "target price",
    "target",
)
COLUNAS_REC = ("recomendação", "recomendacao", "rating")
RECOMENDACOES = {"COMPRA": "Compra", "NEUTRO": "Neutro", "VENDA": "Venda"}
TICKER = re.compile(r"\b([A-Z]{4}\d{1,2})\b")


def _texto(valor: Any) -> str:
    return str(valor).strip() if valor is not None else ""


def _numero(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
        return numero if math.isfinite(numero) else None
    bruto = _texto(valor).replace("R$", "").replace("\xa0", "").strip()
    if not bruto:
        return None
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    try:
        numero = float(bruto)
    except ValueError:
        return None
    return numero if math.isfinite(numero) else None


def _ticker(valor: Any) -> str | None:
    achado = TICKER.search(_texto(valor).upper())
    return achado.group(1) if achado else None


def _coluna(cabecalho: list[str], nomes: tuple[str, ...]) -> int | None:
    for indice, celula in enumerate(cabecalho):
        if any(nome in celula for nome in nomes):
            return indice
    return None


def extrair(conteudo: bytes) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Lê os alvos da planilha; devolve ``{ticker: campos}`` e avisos."""

    try:
        livro = load_workbook(
            io.BytesIO(conteudo), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 - arquivo enviado pelo usuário
        return {}, [f"não foi possível abrir a planilha: {type(exc).__name__}"]

    achados: dict[str, dict[str, Any]] = {}
    avisos: list[str] = []
    abas_com_alvo = 0

    for aba in livro.worksheets:
        linhas = aba.iter_rows(values_only=True)
        cabecalho: list[str] | None = None
        col_ticker = col_alvo = col_rec = None
        for linha in linhas:
            textos = [_texto(celula).lower() for celula in linha]
            ticker = _coluna(textos, COLUNAS_TICKER)
            alvo = _coluna(textos, COLUNAS_ALVO)
            if ticker is not None and alvo is not None:
                cabecalho = textos
                col_ticker, col_alvo = ticker, alvo
                col_rec = _coluna(textos, COLUNAS_REC)
                abas_com_alvo += 1
                break

        if cabecalho is None:
            continue
        assert col_ticker is not None and col_alvo is not None

        for linha in linhas:
            if col_ticker >= len(linha) or col_alvo >= len(linha):
                continue
            ticker = _ticker(linha[col_ticker])
            if not ticker or ticker in achados:
                continue
            alvo = _numero(linha[col_alvo])
            if alvo is None or alvo <= 0:
                continue
            campos: dict[str, Any] = {"btg_alvo": alvo}
            if col_rec is not None and col_rec < len(linha):
                avaliacao = _texto(linha[col_rec]).upper()
                if avaliacao in RECOMENDACOES:
                    campos["btg_recomendacao"] = RECOMENDACOES[avaliacao]
            achados[ticker] = campos

    livro.close()

    if abas_com_alvo == 0:
        return {}, ["não achei uma aba com colunas de ticker e preço-alvo"]
    if not achados:
        return {}, ["a planilha tem cabeçalho, mas nenhuma linha com alvo válido"]
    return achados, avisos


def _preco_atual(ticker: str) -> float | None:
    linhas = db.consultar(
        "SELECT fechamento FROM cotacoes WHERE ticker = ? "
        "ORDER BY data DESC LIMIT 1",
        (ticker,),
    )
    if not linhas:
        return None
    valor = linhas[0]["fechamento"]
    return float(valor) if valor else None


def importar(conteudo: bytes) -> dict[str, Any]:
    """Extrai e grava os alvos; ignora ticker fora do universo ou suspeito."""

    campos_por_ticker, avisos = extrair(conteudo)
    if not campos_por_ticker:
        return {"erro": avisos[0] if avisos else "planilha vazia", "gravados": 0}

    universo = {
        linha["ticker"]
        for linha in db.consultar("SELECT ticker FROM ativos")
    }
    hoje = date.today().isoformat()
    gravados = 0
    campos_gravados = 0
    fora = 0
    suspeitos: list[str] = []
    for ticker, campos in campos_por_ticker.items():
        if ticker not in universo:
            fora += 1
            continue
        preco = _preco_atual(ticker)
        if preco and not (preco / 50 <= campos["btg_alvo"] <= preco * 50):
            suspeitos.append(ticker)
            continue
        linhas = db.gravar_fundos(ticker, hoje, campos, FONTE)
        if linhas:
            gravados += 1
            campos_gravados += linhas

    if suspeitos:
        avisos.append(
            "alvo fora de faixa (provável coluna trocada): "
            + ", ".join(sorted(suspeitos)[:10])
        )
    if fora:
        avisos.append(f"{fora} ticker(s) fora do universo ignorado(s)")

    return {
        "lidos": len(campos_por_ticker),
        "gravados": gravados,
        "campos": campos_gravados,
        "ignorados": fora,
        "suspeitos": len(suspeitos),
        "avisos": avisos,
    }
