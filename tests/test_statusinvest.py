import pytest

from app.fontes import statusinvest


def _html(patrimonio="242.018.551", cotistas="15.069", taxa="0,30 % a.a", liquidez="4.556.764"):
    return f"""
    <html><body>
      <div><h4>Patrimônio líquido</h4><strong>R$&nbsp; {patrimonio}</strong></div>
      <div><h4>Taxa de adm.</h4><span>adm.</span><span>administração</span>
           <strong class="value">{taxa}</strong></div>
      <div>Nº de cotistas {cotistas} Ratio</div>
      <div>Liquidez média diária R$&nbsp; {liquidez}</div>
      <div>CNPJ 38.542.889/0001-01</div>
    </body></html>
    """


def test_extrai_os_campos_do_html(monkeypatch):
    monkeypatch.setattr(statusinvest, "_buscar", lambda ticker: _html())

    campos, avisos = statusinvest.papel("ACWI11")

    assert campos["patrimonio_liquido"] == 242018551.0
    assert campos["cotistas"] == 15069.0
    assert campos["liquidez_diaria_brl"] == 4556764.0
    assert campos["taxa_administracao_etf"] == 0.3
    assert campos["cnpj"] == "38.542.889/0001-01"
    assert avisos == []


def test_campo_publicado_como_travessao_nao_herda_o_numero_seguinte(monkeypatch):
    # GPCA11: "Patrimônio líquido R$ -- Total em carteira R$ -- Liquidez ... 5.161.229"
    html = _html(patrimonio="--", cotistas="--", taxa="-").replace(
        "Total em carteira", "Total em carteira R$ -- Liquidez média diária R$ 5.161.229"
    )
    monkeypatch.setattr(statusinvest, "_buscar", lambda ticker: html)

    campos, avisos = statusinvest.papel("GPCA11")

    assert "patrimonio_liquido" not in campos
    assert "cotistas" not in campos
    assert "taxa_administracao_etf" not in campos
    assert avisos == ["sem taxa de administração na fonte"]


def test_sem_campos_vira_aviso(monkeypatch):
    monkeypatch.setattr(statusinvest, "_buscar", lambda ticker: "<html>nada</html>")

    campos, avisos = statusinvest.papel("XXXX11")

    assert campos == {}
    assert avisos


def test_so_cobre_etf():
    with pytest.raises(ValueError):
        statusinvest.papel("PETR4", "acao_b3")
