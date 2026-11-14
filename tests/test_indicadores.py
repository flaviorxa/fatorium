from datetime import date, timedelta

import pytest

from app.motor import indicadores as ind


def test_pl():
    assert ind.pl(30.0, 3.0) == 10.0
    assert ind.pl(30.0, 0) is None
    assert ind.pl(30.0, -1) is None
    assert ind.pl(30.0, None) is None


def test_pvp():
    assert ind.pvp(30.0, 15.0) == 2.0
    assert ind.pvp(30.0, 0) is None
    assert ind.pvp(30.0, -2) is None


def test_dividend_yield():
    assert ind.dividend_yield(1.0, 20.0) == 5.0
    assert ind.dividend_yield(1.0, 0) is None
    assert ind.dividend_yield(1.0, -1) is None


def test_roe_roic_margem():
    assert ind.roe(20.0, 100.0) == 20.0
    assert ind.roe(20.0, 0) is None
    assert ind.roic(10.0, 50.0) == 20.0
    assert ind.roic(10.0, 0) is None
    assert ind.margem(25.0, 100.0) == 25.0
    assert ind.margem(25.0, 0) is None


def test_nopat():
    assert ind.nopat(100.0) == pytest.approx(66.0)
    assert ind.nopat(100.0, 0.5) == pytest.approx(50.0)
    assert ind.nopat(None) is None


def test_ev():
    assert ind.ev_ebitda(300.0, 100.0) == 3.0
    assert ind.ev_ebitda(300.0, 0) is None
    assert ind.ev_ebitda(300.0, -10) is None
    assert ind.ev_ebit(300.0, 100.0) == 3.0
    assert ind.ev_ebit(300.0, -1) is None
    assert ind.divida_liquida_ebitda(150.0, 100.0) == 1.5
    assert ind.divida_liquida_ebitda(150.0, 0) is None


def test_cagr():
    assert ind.cagr(100.0, 121.0, 2) == pytest.approx(10.0)
    assert ind.cagr(100.0, 100.0, 0) is None
    assert ind.cagr(0.0, 100.0, 2) is None
    assert ind.cagr(100.0, -1.0, 2) is None


def test_payout_e_valor_firma():
    assert ind.payout(50.0, 100.0) == 50.0
    assert ind.payout(50.0, 0) is None
    assert ind.valor_firma(10.0, 100.0, 300.0, 50.0) == 1250.0
    assert ind.valor_firma(None, 100.0, 300.0, 50.0) is None


def test_indicadores_cobre_todas_as_funcoes_publicas():
    funcoes = {
        "pl",
        "pvp",
        "dividend_yield",
        "roe",
        "roic",
        "nopat",
        "ev_ebitda",
        "ev_ebit",
        "divida_liquida_ebitda",
        "margem",
        "cagr",
        "payout",
        "valor_firma",
    }
    assert funcoes <= set(ind.INDICADORES)
    for nome in funcoes:
        detalhe = ind.INDICADORES[nome]
        assert set(detalhe) == {"formula", "unidade", "na_quando"}
        assert callable(getattr(ind, nome))


def test_derivados_ebitda_payout_e_crescimento_receita():
    campos = {
        "preco": 10.0,
        "nro_acoes": 100.0,
        "divida_bruta": 300.0,
        "caixa": 50.0,
        "divida_liquida": 250.0,
        "ev_ebitda": 5.0,
        "lpa": 2.0,
        "crescimento_receita_5a": 8.5,
    }
    pacote = ind.avaliar_fundamentalista(campos, proventos_12m=1.0)

    assert pacote["valor_firma"] == 1250.0
    assert pacote["ebitda"] == pytest.approx(250.0)
    assert pacote["ebitda_derivado"] is True
    assert pacote["ev_ebitda_calculado"] == pytest.approx(5.0)
    assert pacote["divida_liquida_ebitda"] == pytest.approx(1.0)
    assert pacote["payout"] == pytest.approx(50.0)
    assert pacote["crescimento_receita_5a"] == 8.5
    assert pacote["cagr_receita"] is None


def test_payout_usa_proventos_por_acao_sem_dividir_por_acoes():
    serie_proventos = [1.0, 0.50, 0.50]
    pacote = ind.avaliar_fundamentalista(
        {"lpa": 8.0, "nro_acoes": 1_000_000_000.0},
        proventos_12m=sum(serie_proventos),
    )

    assert pacote["payout"] == pytest.approx(25.0)


def test_dy_12m_calculado_usa_ultimo_fechamento():
    pacote = ind.avaliar_fundamentalista(
        {"lpa": 1.0, "preco": 99.0},
        proventos_12m=1.0,
        preco_fechamento=20.0,
    )

    assert pacote["dy_12m_calculado"] == pytest.approx(5.0)
    assert ind.avaliar_fundamentalista({"preco": 20.0})["dy_12m_calculado"] is None


def test_payout_none_sem_proventos_ou_lpa_negativo():
    assert ind.avaliar_fundamentalista({"lpa": 2.0, "nro_acoes": 100.0})["payout"] is None
    assert (
        ind.avaliar_fundamentalista(
            {"lpa": -2.0, "nro_acoes": 100.0}, proventos_12m=100.0
        )["payout"]
        is None
    )


def test_avaliar_fundamentalista_marca_alvo_de_outra_classe():
    duvidoso = ind.avaliar_fundamentalista(
        {"preco": 3.46, "alvo_medio": 24.75}
    )
    assert duvidoso["upside_pct"] is None
    assert duvidoso["alvo_classe_duvidosa"] is True

    legitimo = ind.avaliar_fundamentalista(
        {"preco": 52.00, "alvo_medio": 60.38}
    )
    assert round(legitimo["upside_pct"], 2) == pytest.approx(16.12)
    assert legitimo["alvo_classe_duvidosa"] is False


def test_avaliar_fundamentalista_usa_fechamento_no_upside():
    real = ind.avaliar_fundamentalista(
        {"preco": 17.05, "alvo_medio": 41.83}, preco_fechamento=16.22
    )
    assert real["upside_pct"] is None
    assert real["alvo_classe_duvidosa"] is True

    regressao = ind.avaliar_fundamentalista(
        {"preco": 52.00, "alvo_medio": 60.38}, preco_fechamento=52.00
    )
    assert round(regressao["upside_pct"], 2) == pytest.approx(16.12)
    assert regressao["alvo_classe_duvidosa"] is False

    dentro_faixa = ind.avaliar_fundamentalista(
        {"preco": 10.00, "alvo_medio": 14.00}, preco_fechamento=11.00
    )
    assert round(dentro_faixa["upside_pct"], 2) == pytest.approx(27.27)
    assert dentro_faixa["alvo_classe_duvidosa"] is False


def test_derivados_de_preco_usam_fechamento_quando_existe():
    campos = {
        "preco": 10.0,
        "lpa": 2.0,
        "vpa_calculado": 5.0,
        "nro_acoes": 100.0,
        "divida_bruta": 300.0,
        "caixa": 50.0,
    }
    pacote = ind.avaliar_fundamentalista(
        campos, proventos_12m=2.2, preco_fechamento=11.0
    )

    assert pacote["pl"] == pytest.approx(5.5)
    assert pacote["pvp"] == pytest.approx(2.2)
    assert pacote["valor_firma"] == pytest.approx(1350.0)
    assert pacote["dy_12m_calculado"] == pytest.approx(20.0)


def test_avaliar_fundamentalista_nao_estima():
    campos = {
        "preco": 30.0,
        "lpa": 3.0,
        "vpa_calculado": 15.0,
        "lucro_12m": 20.0,
        "patrimonio_liquido": 100.0,
        "receita_12m": 200.0,
        "ebit_12m": 40.0,
    }
    pacote = ind.avaliar_fundamentalista(campos)
    assert pacote["pl"] == 10.0
    assert pacote["pvp"] == 2.0
    assert pacote["roe"] == 20.0
    assert pacote["margem_liquida"] == pytest.approx(10.0)
    assert pacote["ev_ebitda"] is None
    assert pacote["payout"] is None


def _iso(referencia: date, dias: int) -> str:
    return (referencia - timedelta(days=dias)).isoformat()


def test_retorno_total_12m_soma_cota_e_proventos():
    referencia = date(2026, 1, 1)
    cotacoes = [
        {"data": _iso(referencia, 400), "fechamento": 100.0},
        {"data": _iso(referencia, 1), "fechamento": 110.0},
    ]
    proventos = [
        {"data_ex": _iso(referencia, 30), "valor": 4.0},
        {"data_ex": _iso(referencia, 120), "valor": 2.0},
        {"data_ex": _iso(referencia, 400), "valor": 2.0},
    ]

    resultado = ind.retorno_total_12m(cotacoes, proventos, referencia)

    assert resultado["preco_inicial"] == 100.0
    assert resultado["preco_final"] == 110.0
    assert resultado["data_inicial"] == _iso(referencia, 400)
    assert resultado["data_final"] == _iso(referencia, 1)
    assert resultado["proventos_12m"] == pytest.approx(6.0)
    assert resultado["valorizacao_12m_pct"] == pytest.approx(10.0)
    assert resultado["proventos_12m_pct"] == pytest.approx(6.0)
    assert resultado["retorno_total_12m_pct"] == pytest.approx(16.0)


def test_retorno_total_12m_sem_cotacao_do_corte_fica_none():
    referencia = date(2026, 1, 1)
    cotacoes = [{"data": _iso(referencia, 1), "fechamento": 110.0}]

    resultado = ind.retorno_total_12m(cotacoes, [], referencia)

    assert resultado["preco_inicial"] is None
    assert resultado["data_inicial"] is None
    assert resultado["preco_final"] == 110.0
    assert resultado["data_final"] == _iso(referencia, 1)
    assert resultado["valorizacao_12m_pct"] is None
    assert resultado["proventos_12m_pct"] is None
    assert resultado["retorno_total_12m_pct"] is None


def test_retorno_total_12m_ignora_provento_anterior_ao_limite():
    referencia = date(2026, 1, 1)
    cotacoes = [
        {"data": _iso(referencia, 400), "fechamento": 100.0},
        {"data": _iso(referencia, 1), "fechamento": 100.0},
    ]
    proventos = [
        {"data_ex": _iso(referencia, 366), "valor": 2.0},
        {"data_ex": _iso(referencia, 10), "valor": 1.0},
    ]

    resultado = ind.retorno_total_12m(cotacoes, proventos, referencia)

    assert resultado["proventos_12m"] == pytest.approx(1.0)
    assert resultado["proventos_12m_pct"] == pytest.approx(1.0)
    assert resultado["retorno_total_12m_pct"] == pytest.approx(1.0)


def test_retorno_total_12m_ignora_linhas_invalidas():
    referencia = date(2026, 1, 1)
    cotacoes = [
        {"data": "nao-e-data", "fechamento": 50.0},
        {"data": _iso(referencia, 400), "fechamento": None},
        {"data": _iso(referencia, 390), "fechamento": 0.0},
        {"data": _iso(referencia, 400), "fechamento": 100.0},
        {"data": _iso(referencia, 1), "fechamento": 110.0},
    ]
    proventos = [
        {"data_ex": _iso(referencia, 10), "valor": None},
        {"data_ex": "invalida", "valor": 99.0},
    ]

    resultado = ind.retorno_total_12m(cotacoes, proventos, referencia)

    assert resultado["preco_inicial"] == 100.0
    assert resultado["proventos_12m"] == 0.0
    assert resultado["retorno_total_12m_pct"] == pytest.approx(10.0)


def test_regularidade_proventos_24m_conta_meses_e_meses_dos_dois_anos():
    referencia = date(2026, 11, 10)
    proventos = [
        {"data_ex": "2025-02-10", "valor": 1.0},
        {"data_ex": "2026-02-10", "valor": 1.0},
        {"data_ex": "2025-05-10", "valor": 1.0},
        {"data_ex": "2026-08-10", "valor": 1.0},
        {"data_ex": "2024-11-10", "valor": 9.0},
        {"data_ex": "2026-12-01", "valor": 9.0},
        {"data_ex": "2026-03-01", "valor": None},
    ]

    resultado = ind.regularidade_proventos_24m(proventos, referencia)

    assert resultado["proventos_24m_inicio"] == "2024-12-01"
    assert resultado["regularidade_24m_meses"] == 4
    assert resultado["proventos_24m_qtd"] == 4
    assert resultado["regularidade_24m_pct"] == pytest.approx(16.7)
    assert resultado["proventos_24m_tipicos"] == ["fev"]
    assert resultado["proventos_24m_observacao"] == "paga geralmente em fev"


def test_regularidade_proventos_24m_ativo_mensal_marca_100():
    referencia = date(2026, 12, 31)
    proventos = [
        {"data_ex": f"{ano}-{mes:02d}-05", "valor": 0.1}
        for ano in (2025, 2026)
        for mes in range(1, 13)
    ]

    resultado = ind.regularidade_proventos_24m(proventos, referencia)

    assert resultado["regularidade_24m_meses"] == 24
    assert resultado["proventos_24m_qtd"] == 24
    assert resultado["regularidade_24m_pct"] == pytest.approx(100.0)
    assert resultado["proventos_24m_observacao"] == "paga todos os meses"


def test_regularidade_proventos_24m_sem_pagamento_e_sem_recorrencia():
    referencia = date(2026, 11, 10)

    vazio = ind.regularidade_proventos_24m([], referencia)
    assert vazio["regularidade_24m_meses"] == 0
    assert vazio["regularidade_24m_pct"] == 0.0
    assert vazio["proventos_24m_observacao"] == "sem provento nos últimos 24 meses"

    avulso = ind.regularidade_proventos_24m(
        [
            {"data_ex": "2025-04-10", "valor": 1.0},
            {"data_ex": "2026-08-10", "valor": 1.0},
        ],
        referencia,
    )
    assert avulso["proventos_24m_tipicos"] == []
    assert avulso["proventos_24m_observacao"] == (
        "sem recorrência anual; pagou em abr, ago"
    )
