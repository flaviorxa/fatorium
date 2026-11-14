import pytest

from app.motor import valuation
from app.web import links


def test_bazin_usa_dividendo_anual_sobre_6_por_cento():
    assert valuation.bazin(4.11) == pytest.approx(68.5)
    assert valuation.bazin(4.11, taxa=0.08) == pytest.approx(51.375)


def test_bazin_sem_dividendo_nao_inventa_valor():
    assert valuation.bazin(None) is None
    assert valuation.bazin(0) is None
    assert valuation.bazin(-1.2) is None


def test_graham_raiz_de_22_5_vezes_lpa_vezes_vpa():
    assert valuation.graham(2.0, 10.0) == pytest.approx(21.2132, rel=1e-4)


def test_graham_com_lpa_ou_vpa_nao_positivo_nao_se_aplica():
    assert valuation.graham(-1.0, 10.0) is None
    assert valuation.graham(2.0, -10.0) is None
    assert valuation.graham(None, 10.0) is None


def test_margem_compara_estimativa_com_preco():
    assert valuation.margem(60.0, 50.0) == pytest.approx(20.0)
    assert valuation.margem(None, 50.0) is None
    assert valuation.margem(60.0, 0) is None


def test_acao_tem_msn_investidor10_status_invest_e_genial():
    itens = links.links_externos("vale3", "acao_b3", "https://www.msn.com/x")
    nomes = [item["nome"] for item in itens]
    assert nomes == [
        "MSN Dinheiro",
        "Investidor10",
        "Status Invest",
        "Genial Analisa",
        "BTG Research",
    ]
    assert itens[1]["url"] == "https://investidor10.com.br/acoes/VALE3/"


def test_bdr_usa_a_pasta_de_bdr_no_investidor10():
    itens = links.links_externos("AAPL34", "bdr")
    assert itens[0]["url"] == "https://investidor10.com.br/bdrs/AAPL34/"


def test_fii_tem_funds_explorer_e_nao_tem_genial():
    itens = links.links_externos("HGLG11", "fii")
    nomes = [item["nome"] for item in itens]
    assert "Funds Explorer" in nomes
    assert "Genial Analisa" not in nomes
    assert "https://www.fundsexplorer.com.br/funds/HGLG11" in [
        item["url"] for item in itens
    ]


def test_cripto_e_indice_nao_recebem_link_de_acao():
    assert links.links_externos("BTC-BRL", "cripto") == []
    assert links.links_externos("^BVSP", "indice") == []
