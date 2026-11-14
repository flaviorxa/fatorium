from datetime import date

import numpy as np
import pandas as pd
import pytest

from app import config, db
from app.motor import backtest


@pytest.fixture()
def banco(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _inserir_ativo(
    ticker: str,
    datas,
    fechamentos,
    *,
    campos: dict | None = None,
    data_ref: str | None = None,
    classe: str = "acao_b3",
    setor: str = "Energia",
    proventos: list[dict] | None = None,
) -> None:
    db.upsert_ativo(ticker, nome=f"Empresa {ticker}", classe=classe, setor=setor)
    db.gravar_cotacoes(
        ticker,
        pd.DataFrame(
            {
                "data": datas,
                "abertura": fechamentos,
                "maxima": np.asarray(fechamentos) * 1.01,
                "minima": np.asarray(fechamentos) * 0.99,
                "fechamento": fechamentos,
                "volume": 1_000_000.0,
            }
        ),
    )
    if campos and data_ref:
        db.gravar_fundos(ticker, data_ref, campos)
    if proventos:
        db.gravar_proventos(
            ticker,
            pd.DataFrame(
                [
                    {
                        "data_ex": item["data_ex"],
                        "valor": item["valor"],
                        "tipo": item.get("tipo", "dividendo"),
                    }
                    for item in proventos
                ]
            ),
        )


def _crescente(inicio: str, periodos: int, de: float = 10.0, ate: float = 20.0):
    datas = pd.bdate_range(inicio, periods=periodos)
    return datas, np.linspace(de, ate, periodos)


def test_look_ahead_nao_escolhe_ativo_que_ainda_nao_existia(banco):
    datas_a, precos_a = _crescente("2024-01-02", 420, 10.0, 30.0)
    _inserir_ativo("AAAA3", datas_a, precos_a)

    datas_b, precos_b = _crescente("2025-06-02", 120, 5.0, 40.0)
    _inserir_ativo("BBBB3", datas_b, precos_b)

    resultado = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 9, 30),
        rebalanceamento_meses=1,
        top_n=1,
    )

    escolhidos = {
        ticker
        for carteira in resultado["carteiras"]
        for ticker in carteira["tickers"]
    }
    assert "BBBB3" not in escolhidos
    assert resultado["rebalanceamentos"] >= 2


def test_preco_futuro_nao_muda_a_ultima_carteira(banco):
    datas, precos = _crescente("2024-01-02", 400, 10.0, 25.0)
    _inserir_ativo("AAAA3", datas, precos)
    _inserir_ativo("BBBB3", datas, np.linspace(20.0, 10.0, len(datas)))

    primeira = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 6, 30),
        rebalanceamento_meses=3,
        top_n=1,
    )
    ultima_antes = primeira["carteiras"][-1]

    # Um salto depois da última decisão (abril) não pode alterar a escolha.
    fortes = precos.copy()
    fortes[-40:] = fortes[-40:] * 10
    db.gravar_cotacoes(
        "BBBB3",
        pd.DataFrame(
            {
                "data": datas,
                "abertura": fortes,
                "maxima": fortes,
                "minima": fortes,
                "fechamento": fortes,
                "volume": 1_000_000.0,
            }
        ),
    )

    segunda = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 6, 30),
        rebalanceamento_meses=3,
        top_n=1,
    )

    assert segunda["carteiras"][-1] == ultima_antes


def test_drawdown_maximo_100_80_120(banco):
    assert backtest._drawdown_maximo([100.0, 80.0, 120.0]) == pytest.approx(20.0)


def test_custo_reduz_o_valor_final(banco):
    datas = pd.bdate_range("2024-01-02", periods=500)
    _inserir_ativo("AAAA3", datas, np.linspace(10.0, 80.0, len(datas)))
    _inserir_ativo("BBBB3", datas, np.linspace(30.0, 31.0, len(datas)))

    sem_custo = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 12, 31),
        rebalanceamento_meses=1,
        top_n=2,
        custo_bps=0,
    )
    com_custo = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 12, 31),
        rebalanceamento_meses=1,
        top_n=2,
        custo_bps=10,
    )

    assert com_custo["valor_final"] < sem_custo["valor_final"]
    diferenca_relativa = 1 - com_custo["valor_final"] / sem_custo["valor_final"]
    assert 0 < diferenca_relativa < 0.1


def test_fundamentalista_antes_da_primeira_foto_reduz_periodo(banco):
    datas = pd.bdate_range("2024-01-02", periods=750)
    _inserir_ativo(
        "AAAA3",
        datas,
        np.linspace(8.0, 12.0, len(datas)),
        campos={
            "pl": 8.0,
            "pvp": 1.0,
            "lpa": 1.5,
            "vpa": 12.0,
            "lucro_12m": 100.0,
            "divida_liquida": 50.0,
            "patrimonio_liquido": 500.0,
            "liquidez_corrente": 2.0,
        },
        data_ref="2026-01-02",
    )

    resultado = backtest.simular(
        "graham",
        date(2024, 1, 2),
        date(2026, 10, 28),
        rebalanceamento_meses=3,
    )

    assert resultado["cobertura"]["primeira_foto"] == "2026-01-02"
    assert any(
        "sem foto de fundamentos antes de 2026-01-02" in item
        for item in resultado["limitacoes"]
    )
    assert resultado["inicio"] >= "2026-01-02"
    assert resultado["rebalanceamentos"] >= 1


def test_magic_formula_deriva_valor_firma_asof(banco):
    datas = pd.bdate_range("2024-01-02", periods=500)
    _inserir_ativo(
        "AAAA3",
        datas,
        np.linspace(8.0, 15.0, len(datas)),
        campos={
            "nro_acoes": 1_000.0,
            "divida_bruta": 100.0,
            "caixa": 50.0,
            "ebit_12m": 200.0,
            "patrimonio_liquido": 1_000.0,
            "divida_liquida": 50.0,
        },
        data_ref="2024-12-30",
    )

    resultado = backtest.simular(
        "magic_formula",
        date(2025, 1, 2),
        date(2025, 12, 31),
        rebalanceamento_meses=3,
    )

    assert resultado["carteiras"][0]["tickers"]
    assert any(
        "AAAA3" in carteira["tickers"] for carteira in resultado["carteiras"]
    )
    assert not any(
        "nenhum ativo elegível em" in item for item in resultado["limitacoes"]
    )


def test_trend_template_backtest_nao_inclui_fii_etf_cripto(banco):
    datas = pd.bdate_range("2024-01-02", periods=500)
    _inserir_ativo("AAAA3", datas, np.linspace(10.0, 30.0, len(datas)))
    _inserir_ativo(
        "FIIX11",
        datas,
        np.linspace(10.0, 40.0, len(datas)),
        classe="fii",
        setor=None,
    )
    _inserir_ativo(
        "ETF11",
        datas,
        np.linspace(10.0, 40.0, len(datas)),
        classe="etf",
        setor=None,
    )
    _inserir_ativo(
        "BTC-USD",
        datas,
        np.linspace(10.0, 40.0, len(datas)),
        classe="cripto",
        setor=None,
    )

    resultado = backtest.simular(
        "trend_template",
        date(2025, 1, 2),
        date(2025, 12, 31),
        rebalanceamento_meses=3,
        top_n=10,
    )

    escolhidos = {
        ticker
        for carteira in resultado["carteiras"]
        for ticker in carteira["tickers"]
    }
    assert "AAAA3" in escolhidos
    assert not escolhidos & {"FIIX11", "ETF11", "BTC-USD"}


def test_rebalanceamento_sem_elegiveis_registra_limitacao(banco):
    datas = pd.bdate_range("2024-01-02", periods=500)
    _inserir_ativo(
        "AAAA3",
        datas,
        np.linspace(8.0, 15.0, len(datas)),
        campos={"pl": 10.0},
        data_ref="2024-12-30",
    )

    resultado = backtest.simular(
        "magic_formula",
        date(2025, 1, 2),
        date(2025, 12, 31),
        rebalanceamento_meses=3,
    )

    assert any(
        "nenhum ativo elegível em" in item for item in resultado["limitacoes"]
    )
    assert all(not carteira["tickers"] for carteira in resultado["carteiras"])
    assert (
        "simulação sem carteira: verifique elegibilidade"
        in resultado["limitacoes"]
    )


def test_classes_da_estrategia_usa_a_mesma_regra_do_ranking():
    from app.motor import rankings

    assert rankings.classes_da_estrategia("fiis") == {"fii"}
    assert rankings.classes_da_estrategia("etfs") == {"etf"}
    assert rankings.classes_da_estrategia("trend_template") == {"acao_b3", "bdr"}


def _inserir_fotos_completas(
    quantidade: int = 20,
    *,
    data_ref: str = "2026-10-28",
    ebit: float = 200.0,
    data_inicio: str = "2025-01-02",
    data_fim: str = "2026-10-30",
) -> None:
    datas = pd.bdate_range(data_inicio, data_fim)
    for indice in range(quantidade):
        _inserir_ativo(
            f"COMP{indice:02d}3",
            datas,
            np.linspace(8.0, 12.0, len(datas)),
            campos={
                "ebit_12m": ebit,
                "nro_acoes": 1_000.0,
                "divida_bruta": 100.0,
                "caixa": 50.0,
                "patrimonio_liquido": 1_000.0,
            },
            data_ref=data_ref,
        )


def test_fundamentalista_comeca_na_primeira_foto_completa(banco):
    _inserir_fotos_completas()

    resultado = backtest.simular(
        "magic_formula",
        date(2026, 1, 1),
        date(2026, 10, 30),
        rebalanceamento_meses=1,
    )

    assert resultado["cobertura"]["primeira_foto_completa"] == "2026-10-28"
    assert resultado["inicio_pedido"] == "2026-01-01"
    assert resultado["inicio"] == "2026-10-28"
    assert any(
        "sem foto completa de fundamentos antes de 2026-10-28" in item
        for item in resultado["limitacoes"]
    )
    assert resultado["rebalanceamentos"] >= 1
    assert resultado["carteiras"][0]["tickers"]


def test_fundamentalista_sem_periodo_simulavel_devolve_erro(banco):
    _inserir_fotos_completas()

    resultado = backtest.simular(
        "magic_formula",
        date(2026, 1, 1),
        date(2026, 10, 28),
        rebalanceamento_meses=1,
    )

    assert resultado["erro"] == "sem período simulável"
    assert resultado["inicio_pedido"] == "2026-01-01"
    assert resultado["inicio"] == "2026-10-28"
    assert resultado["retorno_total_pct"] is None
    assert resultado["retorno_anualizado_pct"] is None
    assert resultado["volatilidade_anual_pct"] is None
    assert resultado["drawdown_maximo_pct"] is None
    assert resultado["serie"] == []
    assert any("sem período simulável" in item for item in resultado["limitacoes"])


def test_rebalanceamento_sem_elegiveis_nomeia_o_que_faltou_com_contagem(banco):
    _inserir_fotos_completas(ebit=-1.0)

    resultado = backtest.simular(
        "magic_formula",
        date(2026, 1, 1),
        date(2026, 10, 30),
        rebalanceamento_meses=1,
    )

    limitacao = next(
        item
        for item in resultado["limitacoes"]
        if "nenhum ativo elegível em 2026-10-28" in item
    )
    assert "20" in limitacao

