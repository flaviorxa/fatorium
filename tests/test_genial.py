from datetime import date

from app.fontes import genial

TEXTO_VALE3 = """
Manter
[VALE3]
Preço (30/10)
R$62,02
Preço Alvo
R$86,00
Potencial
+38,66%
Negociar
"""


def test_extrai_alvo_preco_recomendacao_e_potencial():
    campos, avisos = genial.extrair(TEXTO_VALE3, "VALE3")
    assert avisos == []
    assert campos["genial_alvo"] == 86.0
    assert campos["genial_preco"] == 62.02
    assert campos["genial_preco_data"] == f"{date.today().year}-10-30"
    assert campos["genial_recomendacao"] == "Manter"
    assert campos["genial_potencial"] == 38.66


def test_numero_com_milhar_brasileiro():
    texto = "Compra [PETR4] Preço (02/10) R$1.234,50 Preço Alvo R$1.500,00 Potencial +21,5%"
    campos, avisos = genial.extrair(texto, "PETR4")
    assert avisos == []
    assert campos["genial_alvo"] == 1500.0
    assert campos["genial_preco"] == 1234.5


def test_sem_cobertura_nao_grava_alvo():
    campos, avisos = genial.extrair(
        "Preço Alvo --- Potencial --- Sem Cobertura", "AAPL34"
    )
    assert campos == {}
    assert avisos == ["sem cobertura na Genial"]


def test_alvo_ausente_nao_vira_zero():
    campos, avisos = genial.extrair(
        "Manter [VALE3] Preço (30/10) R$62,02 Preço Alvo --- Potencial ---", "VALE3"
    )
    assert campos == {}
    assert avisos == ["sem preço-alvo na página da Genial"]


def test_data_com_ano_explicito():
    assert genial._data("30/10/2025") == "2025-10-30"


def test_pagina_sem_card_avisa():
    campos, avisos = genial.extrair("Relatórios do ativo", "VALE3")
    assert campos == {}
    assert avisos == ["preço-alvo não encontrado na página da Genial"]


def test_ticker_sem_pagina_devolve_sem_cobertura_sem_retentar(monkeypatch):
    chamadas = []

    def baixar(ticker):
        chamadas.append(ticker)
        return None

    monkeypatch.setattr(genial, "_baixar", baixar)

    campos, avisos = genial.preco_alvo("XYZ3")

    assert campos == {}
    assert avisos == ["sem cobertura na Genial"]
    assert chamadas == ["XYZ3"]
