import pandas as pd
import pytest

from app import config, db
from app.fontes.base import Resultado
from scripts import coletar


def _sem_rede(monkeypatch):
    monkeypatch.setattr(
        coletar.fundamentus_br, "papel", lambda ticker, classe: ({}, ["sem dados"])
    )
    monkeypatch.setattr(
        coletar.fundsexplorer, "papel", lambda ticker, classe="fii": ({}, ["sem dados"])
    )
    monkeypatch.setattr(
        coletar.statusinvest, "papel", lambda ticker, classe="etf": ({}, ["sem dados"])
    )
    monkeypatch.setattr(
        coletar.yahoo,
        "fundamentos_basicos",
        lambda ticker: {
            "price_to_book": None,
            "book_value": None,
            "market_cap": None,
            "nome_longo": None,
            "moeda": None,
        },
    )


def test_fii_usa_fonte_alternativa_quando_o_mensal_nao_tem_o_fundo(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _sem_rede(monkeypatch)
    cnpj = "42.730.834/0001-00"
    mapa = {"JURO11": {"cnpj": cnpj}}
    alternativos = {
        cnpj: {
            "cnpj": cnpj,
            "nome": None,
            "data_ref": "2026-10-28",
            "segmento": None,
            "vpa": 100.6518993,
            "patrimonio_liquido": 2070578361.26,
            "cotas": None,
            "cotistas": None,
            "taxa_administracao_informe": None,
            "taxa_administracao_campo": "Percentual_Despesas_Taxa_Administracao",
        }
    }

    resultado = coletar._coletar_um(
        "JURO11", "fii", "2y", {}, mapa, True, alternativos
    )

    assert resultado["fundamentos"] > 0
    vpa = db.consultar(
        "SELECT valor FROM fundamentos WHERE ticker = 'JURO11' AND campo = 'vpa_cvm'"
    )
    assert [dict(linha)["valor"] for linha in vpa] == [100.6518993]
    assert db.consultar("SELECT * FROM falhas WHERE fonte = 'cvm'") == []


def test_fii_usa_o_funds_explorer_e_nao_o_fundamentus(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _sem_rede(monkeypatch)
    chamadas = {}

    def papel_fx(ticker, classe="fii"):
        chamadas["fundsexplorer"] = ticker
        return {"preco": 17.93, "dy": 10.89, "dy_origem": "fundsexplorer"}, []

    monkeypatch.setattr(coletar.fundsexplorer, "papel", papel_fx)

    def papel_fund(*args, **kwargs):
        chamadas["fundamentus"] = True
        return {}, []

    monkeypatch.setattr(coletar.fundamentus_br, "papel", papel_fund)

    coletar._coletar_um("HGBS11", "fii", "2y", {}, {}, True)

    assert chamadas.get("fundsexplorer") == "HGBS11"
    assert "fundamentus" not in chamadas
    dy = db.consultar(
        "SELECT valor FROM fundamentos WHERE ticker = 'HGBS11' AND campo = 'dy' AND fonte = 'fundsexplorer'"
    )
    assert [dict(linha)["valor"] for linha in dy] == [10.89]


def test_etf_usa_o_status_invest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _sem_rede(monkeypatch)

    monkeypatch.setattr(
        coletar.statusinvest,
        "papel",
        lambda ticker, classe="etf": (
            {"patrimonio_liquido": 242018551.0, "taxa_administracao_etf": 0.3},
            [],
        ),
    )

    coletar._coletar_um("ACWI11", "etf", "2y", {}, {}, True)

    linhas = db.consultar(
        "SELECT campo, valor FROM fundamentos WHERE ticker = 'ACWI11' "
        "AND fonte = 'statusinvest' ORDER BY campo"
    )
    gravados = {dict(linha)["campo"]: dict(linha)["valor"] for linha in linhas}
    assert gravados == {
        "patrimonio_liquido": 242018551.0,
        "taxa_administracao_etf": 0.3,
    }


def test_fii_sem_cnpj_mapeado_registra_falha_de_mapeamento(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    monkeypatch.setattr(
        coletar.yahoo,
        "historico",
        lambda ticker, periodo: Resultado(
            ok=False, linhas=0, avisos=[], erro="sem rede"
        ),
    )
    monkeypatch.setattr(
        coletar.yahoo,
        "proventos",
        lambda ticker: pd.DataFrame(columns=["data_ex", "valor"]),
    )
    monkeypatch.setattr(
        coletar.yahoo,
        "fundamentos_basicos",
        lambda ticker: {
            "price_to_book": None,
            "book_value": None,
            "market_cap": None,
            "nome_longo": None,
            "moeda": None,
        },
    )
    monkeypatch.setattr(
        coletar.fundamentus_br, "papel", lambda ticker, classe: ({}, [])
    )

    coletar._coletar_um("SEM01", "fii", "2y", {}, {})

    falhas = db.consultar(
        "SELECT fonte, ticker, etapa, erro FROM falhas WHERE fonte = 'cvm'"
    )
    assert [dict(linha) for linha in falhas] == [
        {
            "fonte": "cvm",
            "ticker": "SEM01",
            "etapa": "mapeamento",
            "erro": "ticker sem CNPJ mapeado",
        }
    ]


def test_indice_registra_o_valor_de_mercado_e_nao_falha(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _sem_rede(monkeypatch)
    db.upsert_ativo("^BVSP", nome="Ibovespa", classe="indice")
    db.gravar_cotacoes(
        "^BVSP",
        pd.DataFrame(
            {
                "data": pd.bdate_range("2026-10-26", periods=3),
                "abertura": [180000.0, 181000.0, 182000.0],
                "maxima": [181000.0, 182000.0, 183000.0],
                "minima": [179000.0, 180000.0, 181000.0],
                "fechamento": [180500.0, 181500.0, 182500.0],
                "volume": [0.0, 0.0, 0.0],
            }
        ),
    )

    resultado = coletar._coletar_um("^BVSP", "indice", "5d", {}, {}, True)

    assert resultado["status"] == "ok"
    valores = {
        dict(linha)["campo"]: dict(linha)["valor"]
        for linha in db.consultar(
            "SELECT campo, valor FROM fundamentos WHERE ticker = '^BVSP'"
        )
    }
    assert valores["preco"] == 182500.0
    # (182.500 / 181.500 - 1) * 100
    assert valores["variacao_pct"] == pytest.approx(0.5509641873278147)


def test_fii_novo_sem_mapa_casa_o_cnpj_pelo_nome(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _sem_rede(monkeypatch)
    cnpj = "11.111.111/0001-11"
    fundos = {
        cnpj: {
            "cnpj": cnpj,
            "nome": "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIARIO",
            "data_ref": "2026-09-01",
            "segmento": "Shoppings",
            "vpa": 20.96,
            "patrimonio_liquido": 3025454936.25,
            "cotas": 144355726.0,
            "cotistas": 205484.0,
            "taxa_administracao_informe": 0.000443,
            "taxa_administracao_campo": "Percentual_Despesas_Taxa_Administracao",
        }
    }
    monkeypatch.setattr(
        coletar.fundsexplorer,
        "papel",
        lambda ticker, classe="fii": ({"dy": 10.89}, []),
    )
    monkeypatch.setattr(
        coletar.yahoo,
        "fundamentos_basicos",
        lambda ticker: {
            "price_to_book": None,
            "book_value": None,
            "market_cap": None,
            "nome_longo": "HEDGE Brasil Shopping Fundo de Investimento Imobiliário",
            "moeda": "BRL",
        },
    )

    resultado = coletar._coletar_um(
        "HGBS11", "fii", "2y", fundos, {}, True
    )

    valores = {
        dict(linha)["campo"]: dict(linha)["valor"]
        for linha in db.consultar(
            "SELECT campo, valor FROM fundamentos WHERE ticker = 'HGBS11' AND fonte = 'cvm'"
        )
    }
    assert valores["vpa_cvm"] == 20.96
    assert any("casado pelo nome" in aviso for aviso in resultado["avisos"])
    assert db.consultar(
        "SELECT * FROM falhas WHERE fonte = 'cvm' AND ticker = 'HGBS11'"
    ) == []
