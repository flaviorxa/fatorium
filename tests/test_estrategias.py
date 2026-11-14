from copy import deepcopy

import pytest

from app.motor import estrategias as est


def _config():
    return deepcopy(est.CONFIG_PADRAO)


def test_magic_formula_nao_classifica_banco():
    ativo = {
        "setor": "Bancos",
        "ebit_12m": 100.0,
        "valor_firma": 1000.0,
        "patrimonio_liquido": 500.0,
        "divida_liquida": 100.0,
    }
    resultado = est.magic_formula(ativo, _config())

    assert resultado["elegivel"] is False
    assert "setor_financeiro" in resultado["reprovados"]
    assert "financeiro" in resultado["motivos"]["setor_financeiro"].lower()


def test_magic_formula_reprova_financeiras_pelo_setor_ou_subsetor():
    financeiras = {
        "PSSA3": ("Previdência e Seguros", "Seguradoras"),
        "ITUB3": ("Intermediários Financeiros", "Bancos"),
        "BBAS3": ("Intermediários Financeiros", "Bancos"),
        "BPAC11": ("Intermediários Financeiros", "Bancos"),
        "BBDC4": ("Intermediários Financeiros", "Bancos"),
    }
    for ticker, (setor, subsetor) in financeiras.items():
        ativo = {
            "ticker": ticker,
            "setor": setor,
            "subsetor": subsetor,
            "ebit_12m": 100.0,
            "valor_firma": 1000.0,
            "capital_investido": 500.0,
        }
        resultado = est.magic_formula(ativo, _config())

        assert resultado["elegivel"] is False, ticker
        assert "setor_financeiro" in resultado["reprovados"], ticker
        assert "setor financeiro" in resultado["motivos"]["setor_financeiro"].lower()

    for ticker, setor, subsetor in (
        ("WEGE3", "Máquinas e Equipamentos", "Motores, Compressores e Outros"),
        ("PETR4", "Petróleo, Gás e Biocombustíveis", "Petróleo e Gás"),
    ):
        ativo = {
            "ticker": ticker,
            "setor": setor,
            "subsetor": subsetor,
            "ebit_12m": 100.0,
            "valor_firma": 1000.0,
            "capital_investido": 500.0,
        }
        resultado = est.magic_formula(ativo, _config())

        assert resultado["elegivel"] is True, ticker
        assert resultado["pontuacao"] is None


def test_magic_formula_ebit_negativo_inelegivel_com_motivo():
    ativo = {
        "setor": "Energia",
        "ebit_12m": -10.0,
        "valor_firma": 1000.0,
        "patrimonio_liquido": 500.0,
        "divida_liquida": 0.0,
    }
    resultado = est.magic_formula(ativo, _config())

    assert resultado["elegivel"] is False
    assert "ebit_positivo" in resultado["reprovados"]
    assert resultado["motivos"]["ebit_positivo"]


def test_graham_reprova_pvp_alto():
    ativo = {
        "pl": 10.0,
        "pvp": 3.0,
        "divida_liquida_patrim": 0.5,
        "lucro_12m": 10.0,
        "liquidez_corrente": 2.0,
        "lpa": 1.0,
        "vpa": 2.0,
    }
    resultado = est.graham(ativo, _config())

    assert resultado["elegivel"] is False
    assert "pvp" in resultado["reprovados"]


def test_bazin_preco_acima_e_abaixo_do_teto():
    config = _config()
    acima = {
        "preco": 20.0,
        "dividendos_medios_por_acao": 1.0,
        "anos_dividendos": 5,
        "dy": 5.0,
    }
    abaixo = {
        "preco": 10.0,
        "dividendos_medios_por_acao": 1.0,
        "anos_dividendos": 5,
        "dy": 5.0,
    }

    assert est.bazin(acima, config)["elegivel"] is False
    resultado = est.bazin(abaixo, config)
    assert resultado["elegivel"] is True
    assert resultado["dados"]["preco_teto"] > abaixo["preco"]


def test_fii_nao_recebe_roe():
    campos = {
        "dy": 8.0,
        "pvp": 0.9,
        "liquidez_diaria_brl": 1_000_000.0,
        "meses_com_provento_24m": 24,
        "roe": 50.0,
    }
    resultado = est.avaliar_fii(campos, _config())

    assert "roe" not in resultado["motivos"]
    todos = (
        resultado["aprovados"]
        + resultado["reprovados"]
        + resultado["indisponiveis"]
    )
    assert all("roe" not in item for item in todos)
    assert "vacancia" in resultado["indisponiveis"]
    assert resultado["motivos"]["vacancia"] == "fonte não fornece"


def test_fii_usa_dy_calculado_e_registra_p_vp_indisponivel():
    campos = {
        "dy_12m_calculado": 9.5,
        "liquidez_diaria_brl": 1_000_000.0,
        "meses_com_provento_24m": 24,
    }
    resultado = est.avaliar_fii(campos, _config())

    assert resultado["elegivel"] is True
    assert resultado["pontuacao"] is not None
    assert 0.0 <= resultado["pontuacao"] <= 100.0
    assert "dy" not in resultado["indisponiveis"]
    assert "p_vp" in resultado["indisponiveis"]
    assert (
        resultado["motivos"]["p_vp"]
        == "fonte não entrega valor patrimonial para este FII"
    )


def test_estrategia_sem_dados_devolve_pontuacao_none():
    resultado = est.graham({}, _config())

    assert resultado["elegivel"] is False
    assert resultado["pontuacao"] is None
    assert resultado["indisponiveis"]


def test_qualidade_registra_conversao_indisponivel_sem_fluxo_de_caixa():
    ativo = {
        "roic": 15.0,
        "roe": 18.0,
        "margem_liquida": 12.0,
        "crescimento_receita_5a": 10.0,
        "divida_liquida_ebitda": 1.0,
    }
    resultado = est.qualidade_crescimento(ativo, _config())

    assert resultado["elegivel"] is True
    assert "conversao_caixa" in resultado["indisponiveis"]
    assert "fluxo de caixa" in resultado["motivos"]["conversao_caixa"]


def test_magic_formula_expoe_ev_ebit_e_ebit_ev_pct_nos_dados():
    ativo = {
        "setor": "Energia",
        "ebit_12m": 100.0,
        "valor_firma": 500.0,
        "capital_investido": 250.0,
    }
    resultado = est.magic_formula(ativo, _config())

    assert resultado["elegivel"] is True
    assert resultado["dados"]["earnings_yield"] == pytest.approx(0.2)
    assert resultado["dados"]["ev_ebit"] == pytest.approx(5.0)
    assert resultado["dados"]["ebit_ev_pct"] == pytest.approx(20.0)


def test_earnings_yield_pontua_percentual():
    ativo = {"ebit_12m": 20.0, "valor_firma": 100.0}
    resultado = est.earnings_yield(ativo, _config())

    assert resultado["elegivel"] is True
    assert resultado["pontuacao"] == 20.0
    assert resultado["dados"]["earnings_yield"] == 0.2
    assert resultado["dados"]["ebit"] == 20.0
    assert resultado["dados"]["valor_firma"] == 100.0


def test_earnings_yield_sem_ev_inelegivel():
    resultado = est.earnings_yield({"ebit_12m": 20.0}, _config())

    assert resultado["elegivel"] is False
    assert "valor_firma" in resultado["indisponiveis"]
    assert resultado["motivos"]["valor_firma"]


def test_earnings_yield_exclui_banco_pelo_ev_nao_representativo():
    banco = {
        "setor": "Bancos",
        "subsetor": "Bancos",
        "ebit_12m": 20.0,
        "valor_firma": 100.0,
    }
    resultado = est.earnings_yield(banco, _config())

    assert resultado["elegivel"] is False
    assert resultado["reprovados"] == ["ev_nao_representativo"]
    assert "P/L × ROE" in resultado["motivos"]["ev_nao_representativo"]


def test_pl_roe_aceita_banco_e_usa_pl_roe_nos_dados():
    banco = {
        "setor": "Bancos",
        "subsetor": "Bancos",
        "pl": 6.0,
        "roe": 22.0,
        "lpa": 3.0,
        "vpa": 18.0,
    }
    resultado = est.pl_roe(banco, _config())

    assert resultado["elegivel"] is True
    assert resultado["dados"]["pl"] == 6.0
    assert resultado["dados"]["roe"] == 22.0
    assert resultado["dados"]["lpa"] == 3.0
    assert resultado["dados"]["vpa"] == 18.0
    assert resultado["dados"]["pl_x_roe"] == pytest.approx(1.32)
    assert resultado["motivos"]["setor"] == (
        "instituição financeira: avaliação por P/L e ROE"
    )


def test_pl_roe_sem_roe_inelegivel():
    resultado = est.pl_roe({"pl": 6.0}, _config())

    assert resultado["elegivel"] is False
    assert "roe" in resultado["indisponiveis"]


def test_motivos_de_fii_mostram_duas_casas_decimais():
    campos = {
        "dy": 10.573690594084187,
        "pvp": 0.9161277603876538,
        "liquidez_diaria_brl": 42_835.2619,
        "meses_com_provento_24m": 23.0,
    }

    resultado = est.avaliar_fii(campos, est.CONFIG_PADRAO)

    assert resultado["motivos"]["dy"] == "DY 12m: 10.57%"
    assert resultado["motivos"]["pvp"] == "P/VP: 0.92"
    assert resultado["motivos"]["regularidade"] == "meses com provento em 24: 23"
