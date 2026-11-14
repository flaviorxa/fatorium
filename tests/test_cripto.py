import pandas as pd
import pytest

from app import config, db
from app.fontes import cripto
from scripts import coletar


def test_universo_converte_cripto_para_par_em_reais(tmp_path):
    lista = tmp_path / "lista.md"
    lista.write_text(
        "## Criptomoedas (1)\n- BTC — Bitcoin\n\n## Ações (1)\n- PETR4\n",
        encoding="utf-8",
    )

    universo = config.ler_universo(lista)

    assert universo["BTC-BRL"] == "cripto"
    assert "BTC-USD" not in universo
    assert universo["PETR4"] == "acao_b3"


def _candle(timestamp: int, abertura, maxima, minima, fechamento, volume) -> list[str]:
    return [
        str(int(timestamp) * 1000),
        str(abertura),
        str(maxima),
        str(minima),
        str(fechamento),
        str(volume),
        "0",
        "0",
        "1",
    ]


def test_historico_vem_em_reais_da_okx(monkeypatch):
    # A OKX devolve do mais novo para o mais antigo e em UTC.
    candles = [
        _candle(1761782400, 590000, 600000, 580000, 590000, 20),
        _candle(1761696000, 600000, 610000, 590000, 600000, 10),
    ]
    chamadas = []

    def falso_get(caminho, **params):
        chamadas.append((caminho, params))
        if caminho == "market/candles":
            return candles
        return []

    monkeypatch.setattr(cripto, "_get", falso_get)

    df = cripto.historico("BTC-USD", "2y")

    assert isinstance(df, pd.DataFrame)
    assert chamadas[0][1]["instId"] == "BTC-BRL"
    assert chamadas[0][1]["bar"] == "1Dutc"
    assert list(df["data"]) == ["2025-10-29", "2025-10-30"]
    # valores do candle, não do par em dólar
    assert df["fechamento"].tolist() == [600000.0, 590000.0]
    assert df["abertura"].tolist() == [600000.0, 590000.0]
    assert df["volume"].tolist() == [10.0, 20.0]


def test_candles_pagina_o_historico_sem_duplicar(monkeypatch):
    def falso_get(caminho, **params):
        if caminho == "market/candles":
            return [_candle(t, 1, 1, 1, 1, 1) for t in (4, 3)]
        assert params["after"] == "3000"
        return [_candle(t, 1, 1, 1, 1, 1) for t in (3, 2, 1)]

    monkeypatch.setattr(cripto, "_get", falso_get)

    linhas = cripto._candles("BTC-BRL", 4)

    assert [linha[0] for linha in linhas] == ["1000", "2000", "3000", "4000"]


def test_spot_cobre_o_dia_que_ainda_nao_fechou(monkeypatch):
    candles = [_candle(1761696000, 600000, 610000, 590000, 600000, 10)]
    spot = [{"instId": "BTC-BRL", "last": "612345.00", "ts": "1761782400000"}]

    def falso_get(caminho, **params):
        if caminho == "market/candles":
            return candles
        if caminho == "market/ticker":
            return spot
        return []

    monkeypatch.setattr(cripto, "_get", falso_get)

    df = cripto.historico("BTC-BRL", "1mo")

    assert list(df["data"]) == ["2025-10-29", "2025-10-30"]
    assert df["fechamento"].iloc[-1] == 612345.0


def test_coleta_de_cripto_usa_a_fonte_em_reais_e_moeda_brl(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    monkeypatch.setattr(
        coletar.fundamentus_br,
        "papel",
        lambda ticker, classe: ({}, ["sem dados"]),
    )

    def sem_yahoo(*args, **kwargs):
        raise AssertionError("cripto não deve passar pelo Yahoo")

    monkeypatch.setattr(coletar.yahoo, "historico", sem_yahoo)
    monkeypatch.setattr(coletar.yahoo, "proventos", sem_yahoo)

    df = pd.DataFrame(
        [
            {
                "data": "2026-10-29",
                "abertura": 430000.0,
                "maxima": 440000.0,
                "minima": 425000.0,
                "fechamento": 435000.0,
                "fechamento_ajustado": 435000.0,
                "volume": 12.0,
            }
        ]
    )
    monkeypatch.setattr(coletar.cripto, "historico", lambda t, p: df)

    resultado = coletar._coletar_um("BTC-BRL", "cripto", "2y")

    assert resultado["barras"] == 1
    assert resultado["erros"] == []
    ativo = dict(
        db.consultar("SELECT moeda, fonte FROM ativos WHERE ticker = 'BTC-BRL'")[0]
    )
    assert ativo == {"moeda": "BRL", "fonte": "okx"}
    linha = dict(
        db.consultar(
            "SELECT fechamento, fonte FROM cotacoes WHERE ticker = 'BTC-BRL'"
        )[0]
    )
    assert linha == {"fechamento": 435000.0, "fonte": "okx"}
