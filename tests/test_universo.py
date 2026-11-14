from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db, universo
from app.api.main import app
from app.motor import rankings
from tests.conftest import CABECALHO_TESTE

LISTA_REAL = config.RAIZ / "config" / "universo.md"


def _copiar_lista(tmp_path, monkeypatch) -> Path:
    destino = tmp_path / "lista-de-ativos.md"
    shutil.copy2(LISTA_REAL, destino)
    monkeypatch.setenv("INVEST_LISTA_ATIVOS", str(destino))
    monkeypatch.setattr(universo, "DIRETORIO_BACKUPS", tmp_path / "backups")
    return destino


def _sem_linhas_alteradas(texto: str, linhas_alteradas: set[str]) -> list[str]:
    return [
        linha
        for linha in texto.splitlines()
        if linha not in linhas_alteradas
    ]


def test_incluir_edita_apenas_a_linha_da_secao_e_o_atualizado(
    tmp_path, monkeypatch
):
    lista = _copiar_lista(tmp_path, monkeypatch)
    antes = lista.read_text(encoding="utf-8")
    cabecalho_antes = next(
        l for l in antes.splitlines() if l.startswith("## Ações (")
    )
    contagem_antes = int(re.search(r"\((\d+)\)", cabecalho_antes).group(1))
    linhas_antes = antes.splitlines()
    indice_cabecalho_antes = linhas_antes.index(cabecalho_antes)
    linha_acoes_antes = linhas_antes[indice_cabecalho_antes + 1]
    resultado = universo.incluir("MGLU3", "acao_b3")
    depois = lista.read_text(encoding="utf-8")

    assert resultado["ticker"] == "MGLU3"
    assert f"## Ações ({contagem_antes + 1})" in depois
    conteudo_depois = next(
        l for l in depois.splitlines() if l.startswith("## Ações (")
    )
    linha_acoes_depois = depois.splitlines()[
        depois.splitlines().index(conteudo_depois) + 1
    ]
    assert linha_acoes_depois.rstrip().endswith(", MGLU3")
    hoje = date.today().strftime("%d/%m/%Y")
    assert f"> Atualizado: {hoje}" in depois
    assert "incluído pela tela" in depois

    atualizado_antes = next(l for l in linhas_antes if l.startswith("> Atualizado:"))
    atualizado_depois = next(
        l for l in depois.splitlines() if l.startswith("> Atualizado:")
    )
    assert _sem_linhas_alteradas(
        antes,
        {cabecalho_antes, linha_acoes_antes, atualizado_antes},
    ) == _sem_linhas_alteradas(
        depois,
        {f"## Ações ({contagem_antes + 1})", linha_acoes_depois, atualizado_depois},
    )

    backups = list((tmp_path / "backups").glob("lista-ativos-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == antes


def test_incluir_duplicado_nao_altera_arquivo(tmp_path, monkeypatch):
    lista = _copiar_lista(tmp_path, monkeypatch)
    antes = lista.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="já está na lista"):
        universo.incluir("PETR4", "acao_b3")

    assert lista.read_text(encoding="utf-8") == antes
    assert list((tmp_path / "backups").glob("*.md")) == []


def test_excluir_remove_da_linha_e_preserva_cotacoes(tmp_path, monkeypatch):
    lista = _copiar_lista(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    db.gravar_cotacoes(
        "PETR4",
        pd.DataFrame(
            [
                {
                    "data": "2026-10-28",
                    "abertura": 10.0,
                    "maxima": 11.0,
                    "minima": 9.0,
                    "fechamento": 10.5,
                    "volume": 1000.0,
                }
            ]
        ),
    )

    antes = lista.read_text(encoding="utf-8")
    cabecalho_antes = next(
        l for l in antes.splitlines() if l.startswith("## Ações (")
    )
    contagem_antes = int(
        re.search(r"\((\d+)\)", cabecalho_antes).group(1)
    )

    resultado = universo.excluir("PETR4")

    depois = lista.read_text(encoding="utf-8")
    assert resultado["contagem"] == contagem_antes - 1
    assert f"## Ações ({contagem_antes - 1})" in depois
    assert "PETR4" not in universo.ler()
    with db.conectar() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM cotacoes WHERE ticker = 'PETR4'"
        ).fetchone()[0]
    assert total == 1
    assert "removido pela tela" in depois


def test_incluir_cria_secao_ausente_antes_do_obs(tmp_path, monkeypatch):
    lista = tmp_path / "lista.md"
    lista.write_text(
        "# Lista\n"
        "> Atualizado: 01/10/2026 — teste\n"
        "\n"
        "## Ações (1)\n"
        "PETR4\n"
        "\n"
        "---\n"
        "*Obs.:* nota final\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INVEST_LISTA_ATIVOS", str(lista))
    monkeypatch.setattr(universo, "DIRETORIO_BACKUPS", tmp_path / "backups")

    universo.incluir("SOL-USD", "cripto")  # legado em dólar vira par em reais

    texto = lista.read_text(encoding="utf-8")
    assert "## Criptomoedas (1)" in texto
    assert "SOL-BRL" in texto
    assert texto.index("## Criptomoedas") < texto.index("---")
    assert "*Obs.:* nota final" in texto


@pytest.mark.parametrize("ticker", ["petro", "PETR4$", "PETR4-USD"])
def test_ticker_invalido_nao_altera_arquivo(tmp_path, monkeypatch, ticker):
    lista = _copiar_lista(tmp_path, monkeypatch)
    antes = lista.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="inválido"):
        universo.incluir(ticker, "acao_b3")

    assert lista.read_text(encoding="utf-8") == antes


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    _copiar_lista(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    db.upsert_ativo("PETR4", nome="Petrobras", classe="acao_b3", setor="Energia")
    db.gravar_cotacoes(
        "PETR4",
        pd.DataFrame(
            [
                {
                    "data": "2026-10-28",
                    "abertura": 10.0,
                    "maxima": 11.0,
                    "minima": 9.0,
                    "fechamento": 10.5,
                    "volume": 1000.0,
                }
            ]
        ),
    )
    with TestClient(app, headers=CABECALHO_TESTE) as test_client:
        yield test_client


def test_pagina_universo_lista_ativos(cliente):
    resposta = cliente.get("/universo")

    assert resposta.status_code == 200
    assert len(resposta.text.splitlines()) >= 126
    assert "Ativos cadastrados" in resposta.text
    assert "PETR4" in resposta.text
    assert "Na lista" in resposta.text


def test_post_incluir_e_excluir_mudam_o_estado(cliente, tmp_path, monkeypatch):
    chamadas = []

    def coletar_falso(tickers, periodo="2y", **_):
        chamadas.append(list(tickers))
        return {"total": len(tickers), "ok": len(tickers), "falhas": 0}

    monkeypatch.setattr("scripts.coletar.coletar", coletar_falso)

    resposta = cliente.post(
        "/universo/incluir",
        data={"ticker": "MGLU3", "classe": "acao_b3"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    aviso = parse_qs(urlsplit(resposta.headers["location"]).query)["aviso"][0]
    assert "MGLU3" in aviso
    assert "coleta iniciada" in aviso
    assert chamadas == [["MGLU3"]]
    with db.conectar() as conn:
        linha = conn.execute(
            "SELECT ativo FROM ativos WHERE ticker = 'MGLU3'"
        ).fetchone()
    assert linha["ativo"] == 1
    assert "MGLU3" in universo.ler()

    resposta = cliente.post(
        "/universo/excluir",
        data={"ticker": "MGLU3"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    aviso = parse_qs(urlsplit(resposta.headers["location"]).query)["aviso"][0]
    assert "removido da lista" in aviso
    with db.conectar() as conn:
        linha = conn.execute(
            "SELECT ativo FROM ativos WHERE ticker = 'MGLU3'"
        ).fetchone()
    assert linha["ativo"] == 0
    assert "MGLU3" not in universo.ler()


def test_inativo_some_dos_rankings_e_do_consolidado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    db.upsert_ativo("AAAA3", nome="Ativa", classe="acao_b3", setor="Energia")
    db.upsert_ativo("BBBB3", nome="Fora", classe="acao_b3", setor="Energia")
    db.marcar_ativo("BBBB3", False)

    datas = pd.bdate_range("2024-01-01", periods=300)
    for ticker in ("AAAA3", "BBBB3"):
        db.gravar_cotacoes(
            ticker,
            pd.DataFrame(
                {
                    "data": datas,
                    "abertura": 10.0,
                    "maxima": 10.1,
                    "minima": 9.9,
                    "fechamento": 10.0,
                    "volume": 1_000_000.0,
                }
            ),
        )
        db.gravar_fundos(
            ticker,
            "2026-10-28",
            {
                "preco": 10.0,
                "pl": 10.0,
                "pvp": 1.0,
                "lpa": 1.0,
                "vpa": 10.0,
                "roe": 20.0,
                "dy": 6.0,
                "lucro_12m": 100.0,
                "patrimonio_liquido": 1000.0,
                "ebit_12m": 200.0,
                "receita_12m": 1000.0,
                "divida_liquida": 100.0,
                "nro_acoes": 100.0,
            },
        )

    carregados = {item["ticker"] for item in rankings._carregar_ativos()}
    assert "AAAA3" in carregados
    assert "BBBB3" not in carregados

    rankings.gerar_rankings()
    resultado = rankings.gerar_rankings()
    assert all(
        item["ticker"] != "BBBB3" for item in resultado["graham"]["itens"]
    )
