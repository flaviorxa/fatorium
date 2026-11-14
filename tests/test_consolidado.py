import json

import pandas as pd
import pytest

from app import config, db
from app.motor import consolidado, rankings


def _banco(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _dados(ticker: str, **extra) -> dict:
    base = {
        "ticker": ticker,
        "nome": f"Empresa {ticker}",
        "classe": "acao_b3",
        "preco": 10.0,
        "data_ref": "2026-10-28",
        "indicadores": {"pl": 10.0, "pvp": 1.0, "dy": 5.0, "roe": 15.0},
    }
    base.update(extra)
    return base


def _gravar_ranking(estrategia: str, itens: list[tuple[str, int]], fora=()):
    with db.conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO rankings (estrategia, gerado_em, config_json, universo_n) "
            "VALUES (?, ?, ?, ?)",
            (estrategia, "2026-10-28", "{}", len(itens) + len(fora)),
        )
        ranking_id = int(cursor.lastrowid)
        for ticker, posicao in itens:
            conn.execute(
                "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao, "
                "dados_json, motivos_json, fora_da_lista) "
                "VALUES (?, ?, ?, ?, ?, '{}', 0)",
                (
                    ranking_id,
                    ticker,
                    posicao,
                    50.0,
                    json.dumps(_dados(ticker), ensure_ascii=False),
                ),
            )
        for ticker in fora:
            conn.execute(
                "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao, "
                "dados_json, motivos_json, fora_da_lista) "
                "VALUES (?, ?, NULL, NULL, ?, '{}', 1)",
                (
                    ranking_id,
                    ticker,
                    json.dumps(
                        {"ticker": ticker, "motivo": "inelegível", "dados": {}},
                        ensure_ascii=False,
                    ),
                ),
            )


def _lista(tickers: list[str]) -> list[tuple[str, int]]:
    return [(ticker, posicao) for posicao, ticker in enumerate(tickers, start=1)]


def test_primeiro_em_tres_listas_supera_terceiro(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    _gravar_ranking("graham", _lista(["AAAA3", "BBBB3", "CCCC3"]))
    _gravar_ranking("bazin", _lista(["AAAA3", "BBBB3", "CCCC3", "DDDD3", "EEEE3"]))
    _gravar_ranking(
        "qualidade",
        _lista(
            ["AAAA3", "BBBB3", "CCCC3", "DDDD3", "EEEE3", "FFFF3", "GGGG3",
             "HHHH3", "IIII3", "JJJJ3"]
        ),
    )

    itens = {item["ticker"]: item for item in consolidado.consolidar()}

    assert itens["AAAA3"]["pontuacao"] == pytest.approx(100.0)
    assert itens["AAAA3"]["pontuacao"] > itens["CCCC3"]["pontuacao"]
    assert itens["CCCC3"]["listas"] == 3
    assert itens["CCCC3"]["posicoes"]["graham"]["percentil"] == pytest.approx(1 / 3)


def test_minimo_de_listas_filtra_presenca_em_duas(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    _gravar_ranking("graham", _lista(["AAAA3", "BBBB3"]))
    _gravar_ranking("bazin", _lista(["AAAA3", "BBBB3"]))

    assert consolidado.consolidar() == []

    itens = {item["ticker"]: item for item in consolidado.consolidar({"min_listas": 2})}
    assert set(itens) == {"AAAA3", "BBBB3"}
    assert itens["AAAA3"]["listas"] == 2


def test_fora_da_lista_nao_pontua_nem_conta_lista(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    _gravar_ranking("graham", _lista(["AAAA3", "CCCC3"]), fora=["BBBB3"])
    _gravar_ranking("bazin", _lista(["BBBB3", "CCCC3"]))
    _gravar_ranking("qualidade", _lista(["BBBB3", "CCCC3"]))

    with_minimo_3 = {item["ticker"]: item for item in consolidado.consolidar()}
    assert "BBBB3" not in with_minimo_3

    with_minimo_2 = {
        item["ticker"]: item for item in consolidado.consolidar({"min_listas": 2})
    }
    assert with_minimo_2["BBBB3"]["listas"] == 2
    assert set(with_minimo_2["BBBB3"]["posicoes"]) == {"bazin", "qualidade"}


def test_posicoes_trazem_posicao_de_e_percentil(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    _gravar_ranking(
        "graham", _lista(["AAAA3", "BBBB3", "CCCC3", "DDDD3"])
    )

    item = next(
        item for item in consolidado.consolidar({"min_listas": 1}) if item["ticker"] == "BBBB3"
    )

    assert item["posicoes"]["graham"]["posicao"] == 2
    assert item["posicoes"]["graham"]["de"] == 4
    assert item["posicoes"]["graham"]["percentil"] == pytest.approx(0.75)


def test_gerar_rankings_grava_consolidado_sem_entrar_nas_ativas(monkeypatch, tmp_path):
    _banco(monkeypatch, tmp_path)
    db.upsert_ativo("AAAA3", nome="Empresa AAAA3", classe="acao_b3", setor="Energia")
    datas = pd.bdate_range("2024-01-01", periods=300)
    db.gravar_cotacoes(
        "AAAA3",
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
    db.gravar_fundos("AAAA3", "2026-10-28", {"pl": 10.0, "pvp": 1.0, "dy": 5.0})

    rankings.gerar_rankings()

    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT COUNT(*) FROM rankings WHERE estrategia = 'consolidado'"
        ).fetchone()[0]
    assert linhas == 1
    assert "consolidado" not in rankings.estrategias_ativas()
    assert "consolidado" not in rankings.ORDEM
