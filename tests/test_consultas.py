from app import config
from app.web.consultas import consenso_resumo, resumo_fontes, valor_item


def test_consenso_resumo_monta_texto_alvo_e_data():
    resumo = consenso_resumo(
        {
            "consenso_total": 13,
            "consenso_compra": 12,
            "consenso_neutro": 1,
            "consenso_venda": 0,
            "alvo_medio": 60.38,
            "preco": 52.0,
            "consenso_data_ref": "2026-10-14",
        }
    )

    assert resumo == {
        "texto": "12C · 1N · 0V",
        "alvo_texto": "R$ 60,38 · +16,1%",
        "alvo_classe_duvidosa": False,
        "data": "14/10/2026",
    }


def test_consenso_resumo_sem_consenso_devolve_none():
    assert consenso_resumo({"ticker": "PETR4", "consenso_total": 0}) is None
    assert consenso_resumo(None) is None


def test_consenso_resumo_nao_mostra_upside_de_outra_classe():
    resumo = consenso_resumo(
        {
            "consenso_total": 13,
            "consenso_compra": 12,
            "consenso_neutro": 1,
            "consenso_venda": 0,
            "alvo_medio": 24.75,
            "preco": 3.46,
            "consenso_data_ref": "2026-10-14",
        }
    )

    assert resumo["alvo_classe_duvidosa"] is True
    assert resumo["alvo_texto"] == (
        "R$ 24,75 · classe em dúvida "
        "(o alvo pode ser da unit/outra classe do papel)"
    )
    assert "+" not in (resumo["alvo_texto"] or "")


def test_valor_item_pula_campo_nulo_e_usa_o_valor_calculado():
    item = {"indicadores": {"dy": None}, "dados": {"dy": 10.573690594084187}}

    assert valor_item(item, "dy") == 10.573690594084187


def test_valor_item_respeita_a_precedencia_quando_o_indicador_tem_valor():
    item = {"indicadores": {"dy": 8.0}, "dados": {"dy": 10.5}}

    assert valor_item(item, "dy") == 8.0


def test_tela_de_fontes_lista_o_funds_explorer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")

    fontes = {item["fonte"]: item for item in resumo_fontes()}

    assert "fundsexplorer" in fontes
    assert "HTML público" in fontes["fundsexplorer"]["limitacao"]
