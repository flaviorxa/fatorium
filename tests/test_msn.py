import json
from datetime import date, timedelta

import pytest

from app import config, db
from app.fontes import msn


def _html(estimate: dict) -> str:
    return (
        "<html><script>window.__DADOS__ = {"
        '"empresa":{"nome":"Petrobras"},'
        f'"estimate":{json.dumps(estimate)}'
        "};</script></html>"
    )


def _usar_banco_temporario(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _estimativa_completa() -> dict:
    return {
        "numberOfAnalysts": 13,
        "recommendationRate": 1.462,
        "recommendation": "strongBuy",
        "numberOfPriceTargets": 13,
        "meanPriceTarget": 60.38,
        "medianPriceTarget": 62,
        "highPriceTarget": 70,
        "lowPriceTarget": 47,
        "analystRecommendation": {
            "strongBuy": 8,
            "buy": 4,
            "hold": 1,
            "sell": 0,
            "underperform": 0,
        },
        "dateLastUpdated": "2026-10-14T11:31:25Z",
        "meanEpsTarget": 13.41,
        "stdDeviationPriceTarget": 5.9,
    }


def test_consenso_parseia_contagem_e_data(monkeypatch):
    monkeypatch.setattr(
        msn, "_baixar_html", lambda url: _html(_estimativa_completa())
    )

    campos, avisos = msn.consenso("PETR4")

    assert campos["consenso_total"] == 13
    assert campos["consenso_compra"] == 12
    assert campos["consenso_neutro"] == 1
    assert campos["consenso_venda"] == 0
    assert campos["alvo_medio"] == 60.38
    assert campos["alvo_mediana"] == 62.0
    assert campos["alvo_max"] == 70.0
    assert campos["alvo_min"] == 47.0
    assert campos["recomendacao"] == "Compra forte"
    assert campos["taxa_recomendacao"] == 1.462
    assert campos["data_ref"] == "2026-10-14"
    assert avisos == []


def test_consenso_sem_contagem_por_analista(monkeypatch):
    estimativa = {
        "numberOfAnalysts": 5,
        "recommendationRate": 2.1,
        "recommendation": "buy",
        "meanPriceTarget": 40.0,
        "dateLastUpdated": date.today().isoformat() + "T10:00:00Z",
    }
    monkeypatch.setattr(msn, "_baixar_html", lambda url: _html(estimativa))

    campos, avisos = msn.consenso("PETR4")

    assert campos["consenso_total"] == 5
    assert "consenso_compra" not in campos
    assert campos["alvo_medio"] == 40.0
    assert "sem contagem por analista na fonte" in avisos


def test_consenso_com_um_analista_nao_e_consenso(monkeypatch):
    estimativa = _estimativa_completa()
    estimativa["numberOfAnalysts"] = 1
    estimativa["analystRecommendation"] = {
        "strongBuy": 1,
        "buy": 0,
        "hold": 0,
        "sell": 0,
        "underperform": 0,
    }
    estimativa["dateLastUpdated"] = date.today().isoformat() + "T10:00:00Z"
    monkeypatch.setattr(msn, "_baixar_html", lambda url: _html(estimativa))

    _, avisos = msn.consenso("PETR4")

    assert "consenso com 1 analista: não é consenso" in avisos


def test_consenso_antigo_gera_aviso(monkeypatch):
    antiga = date.today() - timedelta(days=300)
    estimativa = _estimativa_completa()
    estimativa["dateLastUpdated"] = antiga.isoformat() + "T10:00:00Z"
    monkeypatch.setattr(msn, "_baixar_html", lambda url: _html(estimativa))

    campos, avisos = msn.consenso("PETR4")

    assert campos["data_ref"] == antiga.isoformat()
    assert f"consenso desatualizado desde {antiga.isoformat()}" in avisos


def test_ticker_fora_do_mapa_registra_falha_sem_excecao(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    monkeypatch.setattr(msn, "_ler_mapa", lambda: {})

    campos, avisos = msn.consenso("SEM99")

    assert campos == {}
    assert avisos == ["ticker sem página no MSN"]
    falhas = db.consultar(
        "SELECT fonte, ticker, etapa, erro FROM falhas WHERE fonte = 'msn'"
    )
    assert [dict(linha) for linha in falhas] == [
        {
            "fonte": "msn",
            "ticker": "SEM99",
            "etapa": "mapeamento",
            "erro": "ticker sem página no MSN",
        }
    ]


def test_upside_usa_preco_atual():
    assert round(msn.upside_pct(60.38, 52.00), 2) == pytest.approx(16.12)
    assert msn.alvo_classe_duvidosa(60.38, 52.00) is False
    assert msn.upside_pct(60.38, None) is None
    assert msn.alvo_classe_duvidosa(60.38, None) is False
    assert msn.upside_pct(None, 52.00) is None
    assert msn.upside_pct(60.38, 0) is None


def test_alvo_muito_acima_do_preco_fica_duvidoso():
    assert msn.upside_pct(24.75, 3.46) is None
    assert msn.alvo_classe_duvidosa(24.75, 3.46) is True
    assert msn.upside_pct(37.37, 13.41) is None
    assert msn.alvo_classe_duvidosa(37.37, 13.41) is True


def test_limite_de_2_5_vezes_e_inclusivo():
    assert msn.upside_pct(25.0, 10.0) == pytest.approx(150.0)
    assert msn.alvo_classe_duvidosa(25.0, 10.0) is False
    assert msn.upside_pct(25.1, 10.0) is None
    assert msn.alvo_classe_duvidosa(25.1, 10.0) is True


def test_limite_inferior_de_0_5_vezes_e_inclusivo():
    assert msn.upside_pct(5.0, 10.0) == pytest.approx(-50.0)
    assert msn.alvo_classe_duvidosa(5.0, 10.0) is False
    assert msn.upside_pct(4.99, 10.0) is None
    assert msn.alvo_classe_duvidosa(4.99, 10.0) is True
