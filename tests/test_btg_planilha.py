import io

import pandas as pd
import pytest
from openpyxl import Workbook

from app import config, db
from app.fontes import btg_planilha


def _planilha(linhas: list[list], cabecalho: list | None = None) -> bytes:
    livro = Workbook()
    aba = livro.worksheets[0]
    aba.title = "Resumo"
    aba.append(["BTG Pactual — Stock Guide"])
    aba.append([])
    aba.append(cabecalho or ["Ticker", "Empresa", "Preço-alvo (R$)", "Recomendação"])
    for linha in linhas:
        aba.append(linha)
    ruido = livro.create_sheet("Notas")
    ruido.append(["Este mês mantemos a carteira"])
    buffer = io.BytesIO()
    livro.save(buffer)
    return buffer.getvalue()


def test_le_ticker_alvo_e_recomendacao():
    conteudo = _planilha(
        [
            ["VALE3", "Vale", 90.0, "COMPRA"],
            ["PETR4", "Petrobras", "R$ 65,00", "NEUTRO"],
        ]
    )
    campos, avisos = btg_planilha.extrair(conteudo)

    assert avisos == []
    assert campos["VALE3"] == {"btg_alvo": 90.0, "btg_recomendacao": "Compra"}
    assert campos["PETR4"] == {"btg_alvo": 65.0, "btg_recomendacao": "Neutro"}


def test_aceita_ticker_com_sufixo_e_ignora_duplicado():
    conteudo = _planilha(
        [
            ["VALE3 BZ Equity", "Vale", "90,00", "COMPRA"],
            ["VALE3", "Vale repetida", 99.0, "COMPRA"],
        ]
    )
    campos, _ = btg_planilha.extrair(conteudo)
    assert campos["VALE3"]["btg_alvo"] == 90.0


def test_planilha_sem_cabecalho_avisa():
    conteudo = _planilha([["VALE3", "Vale", 90.0]], cabecalho=["Papel", "Empresa"])
    campos, avisos = btg_planilha.extrair(conteudo)
    assert campos == {}
    assert avisos == ["não achei uma aba com colunas de ticker e preço-alvo"]


def test_arquivo_que_nao_e_xlsx_nao_levanta():
    campos, avisos = btg_planilha.extrair(b"nao e planilha")
    assert campos == {}
    assert avisos and avisos[0].startswith("não foi possível abrir a planilha")


def test_importar_grava_so_o_que_esta_no_universo(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    db.upsert_ativo("VALE3", nome="Vale", classe="acao_b3")
    db.gravar_cotacoes(
        "VALE3",
        pd.DataFrame(
            {
                "data": ["2026-10-30"],
                "fechamento": [60.0],
            }
        ),
    )
    db.upsert_ativo("PETR4", nome="Petrobras", classe="acao_b3")
    conteudo = _planilha(
        [
            ["VALE3", "Vale", 90.0, "COMPRA"],
            ["ZZZZ3", "Fora do universo", 10.0, None],
            ["PETR4", "Sem cotação no banco", 40.0, None],
        ]
    )

    resumo = btg_planilha.importar(conteudo)

    assert resumo["gravados"] == 2
    assert resumo["ignorados"] == 1
    linhas = db.consultar(
        "SELECT ticker, valor FROM fundamentos "
        "WHERE fonte = 'btg_planilha' AND campo = 'btg_alvo' ORDER BY ticker"
    )
    assert [(linha["ticker"], linha["valor"]) for linha in linhas] == [
        ("PETR4", 40.0),
        ("VALE3", 90.0),
    ]
