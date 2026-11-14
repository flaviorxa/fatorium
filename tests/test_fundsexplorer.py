import json

import pytest

from app.fontes import fundsexplorer


def _html(meta: dict) -> str:
    """Página mínima com o mesmo formato do site (JSON num script)."""

    conteudo = {"pagePostTerms": {"category": ["Tijolo"], "meta": meta}}
    return (
        "<html><head><script>\n"
        "\tvar dataLayer_content = "
        + json.dumps(conteudo)
        + ";\n\tdataLayer.push( dataLayer_content );\n</script></head></html>"
    )


def test_le_os_campos_do_blob_embutido(monkeypatch):
    texto = _html(
        {
            "codigo": "HGBS11",
            "valor": 17.93,
            "dy": 10.89,
            "max_52_semanas": 20.78,
            "min_52_semanas": 15.1,
            "liquidezmediadiaria": 9507422.14,
            "assets_number": 15,
            "valorpatrimonialcota": 20.18,
        }
    )
    monkeypatch.setattr(fundsexplorer, "_buscar", lambda ticker: fundsexplorer._meta_do_html(texto))

    campos, avisos = fundsexplorer.papel("HGBS11")

    assert campos["preco"] == 17.93
    assert campos["dy"] == 10.89
    assert campos["max_52s"] == 20.78
    assert campos["liquidez_diaria_brl"] == 9507422.14
    assert campos["imoveis"] == 15
    assert campos["dy_origem"] == "fundsexplorer"
    # valor patrimonial é da CVM; a fonte não deve gravar vpa_cvm
    assert "vpa_cvm" not in campos
    assert avisos == []


def test_vacancia_usa_o_periodo_mais_recente(monkeypatch):
    texto = _html(
        {
            "valor": 17.93,
            "dy": 10.89,
            "vacancia_0_vacancia_fisica": 6.1,
            "vacancia_0_periodo": 20160401,
            "vacancia_1_vacancia_fisica": 4.4,
            "vacancia_1_periodo": 20260801,
        }
    )
    monkeypatch.setattr(fundsexplorer, "_buscar", lambda ticker: fundsexplorer._meta_do_html(texto))

    campos, _ = fundsexplorer.papel("HGBS11")

    assert campos["vacancia"] == 4.4
    assert campos["vacancia_data_ref"] == "2026-08-01"


def test_sem_dy_avisa_sem_inventar_valor(monkeypatch):
    texto = _html({"valor": 91.19, "max_52_semanas": 98.71})
    monkeypatch.setattr(fundsexplorer, "_buscar", lambda ticker: fundsexplorer._meta_do_html(texto))

    campos, avisos = fundsexplorer.papel("AZIN11")

    assert "dy" not in campos
    assert avisos == ["sem DY no Funds Explorer"]


def test_bloco_ausente_vira_aviso_nao_excecao(monkeypatch):
    monkeypatch.setattr(fundsexplorer, "_buscar", lambda ticker: fundsexplorer._meta_do_html("<html></html>"))

    campos, avisos = fundsexplorer.papel("XXXX11")

    assert campos == {}
    assert avisos and "dataLayer_content" in avisos[0]


def test_so_cobre_fii():
    with pytest.raises(ValueError):
        fundsexplorer.papel("PETR4", "acao_b3")
