from app.fontes import btg

HTML_VALE3 = """
<div class="current-price-present"><span>R$</span> 62,02 </div>
</div><div class="target-price"><p>Preço-alvo</p>
<p class="target-price-present"><span>R$</span> 90,00 </p></div>
<script>window.state = {"asset":{"ticker":"VALE3"},
"recommendation":{"targetPrice":"90.00000000000",
"recommendation":"COMPRA",
"recommendationDate":"2026-10-30T09:29:16.489Z",
"informationSource":"research-btg"}};</script>
"""


def test_extrai_alvo_recomendacao_e_data():
    campos, avisos = btg.extrair(HTML_VALE3)
    assert avisos == []
    assert campos == {
        "btg_alvo": 90.0,
        "btg_recomendacao": "Compra",
        "btg_data": "2026-10-30",
    }


def test_recomendacao_neutra_e_traduzida():
    campos, _ = btg.extrair(
        '{"recommendation":{"targetPrice":"53.0","recommendation":"NEUTRO",'
        '"recommendationDate":"2026-10-30T09:29:16.489Z"}}'
    )
    assert campos["btg_recomendacao"] == "Neutro"


def test_sem_objeto_e_sem_cobertura():
    campos, avisos = btg.extrair("<html>página sem card</html>")
    assert campos == {}
    assert avisos == ["sem cobertura no BTG"]


def test_alvo_ausente_ou_invalido_nao_vira_zero():
    campos, avisos = btg.extrair(
        '{"recommendation":{"targetPrice":"0","recommendation":"COMPRA"}}'
    )
    assert campos == {}
    assert avisos == ["sem preço-alvo no BTG"]


def test_json_quebrado_nao_levanta():
    campos, avisos = btg.extrair('{"recommendation":{"targetPrice":')
    assert campos == {}
    assert avisos == ["recommendation ilegível no HTML do BTG"]
