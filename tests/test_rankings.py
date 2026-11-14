import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app import config, db
from app.motor import indicadores as ind
from app.motor import rankings


def _usar_banco_temporario(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _inserir_ativo(
    ticker: str,
    preco: float = 20.0,
    classe: str = "acao_b3",
    setor: str | None = "Energia",
    campos_extra: dict | None = None,
    proventos: list[dict] | None = None,
):
    db.upsert_ativo(
        ticker, nome=f"Empresa {ticker}", classe=classe, setor=setor
    )

    datas = pd.bdate_range("2024-01-01", periods=300)
    fechamento = np.linspace(preco * 0.7, preco, len(datas))
    cotacoes = pd.DataFrame(
        {
            "data": datas,
            "abertura": fechamento,
            "maxima": fechamento * 1.01,
            "minima": fechamento * 0.99,
            "fechamento": fechamento,
            "volume": 1_000_000.0,
        }
    )
    db.gravar_cotacoes(ticker, cotacoes)

    campos = {
        "preco": preco,
        "pl": 10.0,
        "pvp": 1.0,
        "lpa": 1.0,
        "vpa": preco,
        "lucro_12m": 100.0,
        "patrimonio_liquido": 1000.0,
        "ebit_12m": 200.0,
        "ebitda": 100.0,
        "receita_12m": 1000.0,
        "divida_liquida": 100.0,
        "divida_bruta": 200.0,
        "caixa": 100.0,
        "nro_acoes": 100.0,
        "liquidez_corrente": 2.0,
        "roe": 20.0,
        "roic": 15.0,
        "margem_liquida": 10.0,
        "crescimento_receita_5a": 8.0,
        "dy": 6.0,
    }
    if campos_extra:
        campos.update(campos_extra)
    db.gravar_fundos(ticker, "2026-10-28", campos)

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


def _proventos_anuais(valores: list[float], inicio: int = 2022) -> list[dict]:
    return [
        {"data_ex": f"{inicio + indice}-06-01", "valor": valor}
        for indice, valor in enumerate(valores)
    ]


def _inserir_ativo_magic(ticker: str, ebit: float, ev: float, capital: float):
    db.upsert_ativo(ticker, nome=f"Empresa {ticker}", classe="acao_b3", setor="Energia")

    datas = pd.bdate_range("2024-01-01", periods=300)
    fechamento = np.linspace(0.7, 1.0, len(datas))
    cotacoes = pd.DataFrame(
        {
            "data": datas,
            "abertura": fechamento,
            "maxima": fechamento * 1.01,
            "minima": fechamento * 0.99,
            "fechamento": fechamento,
            "volume": 1_000_000.0,
        }
    )
    db.gravar_cotacoes(ticker, cotacoes)

    campos = {
        "preco": 1.0,
        "nro_acoes": ev,
        "divida_bruta": 0.0,
        "caixa": 0.0,
        "ebit_12m": ebit,
        "capital_investido": capital,
    }
    db.gravar_fundos(ticker, "2026-10-28", campos)


def test_magic_formula_usa_soma_de_postos(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo_magic("AAAA3", ebit=10.0, ev=50.0, capital=50.0)
    _inserir_ativo_magic("BBBB3", ebit=10.0, ev=100.0, capital=100.0)
    _inserir_ativo_magic("CCCC3", ebit=10.0, ev=200.0, capital=200.0)

    resultado = rankings.gerar_rankings()["magic_formula"]["itens"]

    assert [item["ticker"] for item in resultado[:3]] == ["AAAA3", "BBBB3", "CCCC3"]
    por_ticker = {item["ticker"]: item for item in resultado}
    assert por_ticker["AAAA3"]["pontuacao"] == 2
    assert por_ticker["AAAA3"]["dados"]["posto_ey"] == 1
    assert por_ticker["AAAA3"]["dados"]["posto_roc"] == 1
    assert por_ticker["CCCC3"]["pontuacao"] == 6

    with db.conectar() as conn:
        config_json = conn.execute(
            "SELECT config_json FROM rankings WHERE estrategia = 'magic_formula' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "soma de postos, menor é melhor" in config_json


def test_geracao_grava_itens_e_preserva_historico(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo("AAAA3")

    primeiro = rankings.gerar_rankings()
    assert any(item["ticker"] == "AAAA3" for item in primeiro["graham"]["itens"])

    with db.conectar() as conn:
        total_rankings = conn.execute(
            "SELECT COUNT(*) FROM rankings WHERE estrategia = 'graham'"
        ).fetchone()[0]
        total_itens = conn.execute(
            "SELECT COUNT(*) FROM ranking_itens WHERE ranking_id = "
            "(SELECT MAX(id) FROM rankings WHERE estrategia = 'graham')"
        ).fetchone()[0]
    assert total_rankings == 1
    assert total_itens == 1

    _inserir_ativo("BBBB3")
    rankings.gerar_rankings()

    with db.conectar() as conn:
        total_rankings = conn.execute(
            "SELECT COUNT(*) FROM rankings WHERE estrategia = 'graham'"
        ).fetchone()[0]
    assert total_rankings == 2
    assert "BBBB3" in rankings.comparar_com_anterior("graham")["entrou"]


def test_comparar_identifica_saiu(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo("AAAA3")
    _inserir_ativo("BBBB3")
    rankings.gerar_rankings()

    with db.conectar() as conn:
        conn.execute("DELETE FROM ativos WHERE ticker = 'AAAA3'")

    rankings.gerar_rankings()
    comparacao = rankings.comparar_com_anterior("graham")

    assert "AAAA3" in comparacao["saiu"]
    assert "AAAA3" not in comparacao["entrou"]


def test_posicoes_contiguas_em_todas_as_estrategias(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "AAAA3", preco=10.0, proventos=_proventos_anuais([1, 1, 1, 1, 1])
    )
    _inserir_ativo(
        "AAPL34",
        preco=30.0,
        classe="bdr",
        setor="Tecnologia",
        proventos=_proventos_anuais([1, 1, 1, 1, 1]),
    )
    _inserir_ativo(
        "FIIX11",
        preco=100.0,
        classe="fii",
        setor=None,
        campos_extra={
            "dy": 9.0,
            "pvp": 0.9,
            "liquidez_diaria_brl": 1_000_000.0,
            "meses_com_provento_24m": 24,
        },
    )
    _inserir_ativo("ETF11", preco=50.0, classe="etf", setor=None)
    db.upsert_ativo("BTC-USD", nome="Bitcoin", classe="cripto")

    resultado = rankings.gerar_rankings()

    for estrategia in rankings.ORDEM:
        dados = resultado[estrategia]
        itens = dados["itens"]
        posicoes = [item["posicao"] for item in itens]
        assert posicoes == list(range(1, len(itens) + 1)), estrategia
        assert all(posicao is not None for posicao in posicoes), estrategia
        assert all("posicao" not in item for item in dados["fora_da_lista"])


def test_graham_ordena_por_margem_de_seguranca(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    vpa = 10.0
    preco = 10.0
    # margem = sqrt(22.5 * LPA * VPA) / preco - 1
    lpa_10 = ((preco * 1.10) ** 2) / (22.5 * vpa)
    lpa_40 = ((preco * 1.40) ** 2) / (22.5 * vpa)
    _inserir_ativo(
        "AAAA3",
        preco=preco,
        campos_extra={"vpa_calculado": vpa, "lpa": lpa_10},
    )
    _inserir_ativo(
        "ZZZZ3",
        preco=preco,
        campos_extra={"vpa_calculado": vpa, "lpa": lpa_40},
    )

    itens = rankings.gerar_rankings()["graham"]["itens"]

    assert [item["ticker"] for item in itens[:2]] == ["ZZZZ3", "AAAA3"]
    assert itens[0]["posicao"] == 1
    assert itens[0]["pontuacao"] == pytest.approx(40.0)
    assert itens[1]["pontuacao"] == pytest.approx(10.0)


def test_bazin_mediana_nao_explode_com_provento_extraordinario(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "AAAA3", preco=10.0, proventos=_proventos_anuais([1, 1, 1, 1, 1])
    )
    _inserir_ativo(
        "BBBB3", preco=12.0, proventos=_proventos_anuais([1, 1, 1, 1, 10])
    )

    itens = rankings.gerar_rankings()["bazin"]["itens"]
    por_ticker = {item["ticker"]: item for item in itens}

    assert itens[0]["ticker"] == "AAAA3"
    assert por_ticker["BBBB3"]["posicao"] != 1
    assert por_ticker["BBBB3"]["dados"]["dividendos_medios_por_acao"] == 1.0
    assert (
        por_ticker["BBBB3"]["destaque"]
        == "último provento acima de 2× a mediana"
    )


def test_trend_template_exclui_etf_fii_cripto(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo("AAAA3", preco=10.0)
    _inserir_ativo("AAPL34", preco=30.0, classe="bdr", setor="Tecnologia")
    _inserir_ativo(
        "FIIX11",
        preco=100.0,
        classe="fii",
        setor=None,
        campos_extra={
            "dy": 9.0,
            "pvp": 0.9,
            "liquidez_diaria_brl": 1_000_000.0,
            "meses_com_provento_24m": 24,
        },
    )
    _inserir_ativo("ETF11", preco=50.0, classe="etf", setor=None)
    db.upsert_ativo("BTC-USD", nome="Bitcoin", classe="cripto")

    dados = rankings.gerar_rankings()["trend_template"]
    tickers = {item["ticker"] for item in dados["itens"]}

    assert "FIIX11" not in tickers
    assert "ETF11" not in tickers
    assert "BTC-USD" not in tickers
    assert tickers <= {"AAAA3", "AAPL34"}

    motivo = "classe sem histórico compatível com o Trend Template"
    fora = {item["ticker"]: item for item in dados["fora_da_lista"]}
    for ticker in ("FIIX11", "ETF11", "BTC-USD"):
        assert fora[ticker]["motivo"] == motivo


def test_lista_de_fiis_do_banco_de_fixture_passa_de_15(monkeypatch, tmp_path):
    origem = config.RAIZ / "data" / "invest.db"
    if not origem.exists():
        pytest.skip("banco de fixture data/invest.db ausente")

    with sqlite3.connect(origem) as fonte:
        fiis = fonte.execute(
            "SELECT COUNT(*) FROM ativos WHERE classe = 'fii'"
        ).fetchone()[0]
    if not fiis:
        pytest.skip("banco de fixture sem FIIs")

    destino = tmp_path / "invest.db"
    with sqlite3.connect(origem) as fonte, sqlite3.connect(destino) as copia:
        fonte.backup(copia)
    monkeypatch.setattr(config, "DB_PATH", destino)

    itens = rankings.gerar_rankings()["fiis"]["itens"]

    assert len(itens) > 15


def test_fii_sem_pvp_pontua_normalizado(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "FIIX11",
        preco=100.0,
        classe="fii",
        setor=None,
        campos_extra={
            "dy": 9.5,
            "pvp": None,
            "liquidez_diaria_brl": 1_000_000.0,
            "meses_com_provento_24m": 24,
        },
    )

    itens = rankings.gerar_rankings()["fiis"]["itens"]
    item = next(item for item in itens if item["ticker"] == "FIIX11")

    assert item["pontuacao"] is not None
    assert 0.0 <= item["pontuacao"] <= 100.0
    assert "p_vp" in item["indisponiveis"]


def _inserir_fii_ltm(
    ticker: str,
    preco_final: float,
    *,
    preco_inicial: float = 100.0,
    com_historico: bool = True,
    proventos: list[dict] | None = None,
    dy: float = 8.0,
):
    db.upsert_ativo(ticker, nome=f"Fundo {ticker}", classe="fii", setor=None)

    hoje = date.today()
    linhas = []
    if com_historico:
        linhas.append(
            {
                "data": hoje - timedelta(days=400),
                "abertura": preco_inicial,
                "maxima": preco_inicial,
                "minima": preco_inicial,
                "fechamento": preco_inicial,
                "volume": 1_000_000.0,
            }
        )
    linhas.append(
        {
            "data": hoje - timedelta(days=1),
            "abertura": preco_final,
            "maxima": preco_final,
            "minima": preco_final,
            "fechamento": preco_final,
            "volume": 1_000_000.0,
        }
    )
    db.gravar_cotacoes(ticker, pd.DataFrame(linhas))
    db.gravar_fundos(
        ticker,
        hoje.isoformat(),
        {
            "dy": dy,
            "pvp": 1.0,
            "liquidez_diaria_brl": 1_000_000.0,
            "meses_com_provento_24m": 24,
        },
    )
    if proventos:
        db.gravar_proventos(
            ticker,
            pd.DataFrame(
                [
                    {
                        "data_ex": item["data_ex"],
                        "valor": item["valor"],
                        "tipo": item.get("tipo", "rendimento"),
                    }
                    for item in proventos
                ]
            ),
        )


def test_fiis_ltm_ordena_por_retorno_total_e_exclui_sem_12_meses(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_fii_ltm("AAAA11", preco_final=120.0)
    _inserir_fii_ltm("BBBB11", preco_final=110.0)
    _inserir_fii_ltm("CCCC11", preco_final=90.0)
    _inserir_fii_ltm("DDDD11", preco_final=100.0, com_historico=False)

    dados = rankings.gerar_rankings()["fiis_ltm"]

    assert [item["ticker"] for item in dados["itens"]] == [
        "AAAA11",
        "BBBB11",
        "CCCC11",
    ]
    assert [item["posicao"] for item in dados["itens"]] == [1, 2, 3]
    assert [item["pontuacao"] for item in dados["itens"]] == pytest.approx(
        [20.0, 10.0, -10.0]
    )
    assert dados["itens"][0]["dados"]["retorno_total_12m_pct"] == pytest.approx(20.0)
    assert dados["itens"][0]["dados"]["valorizacao_12m_pct"] == pytest.approx(20.0)
    assert dados["itens"][0]["dados"]["preco_inicial"] == pytest.approx(100.0)

    fora = {item["ticker"]: item for item in dados["fora_da_lista"]}
    assert fora["DDDD11"]["motivo"] == "sem cotação de 12 meses atrás"


def test_fiis_ltm_soma_proventos_da_janela_ao_retorno(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    hoje = date.today()
    _inserir_fii_ltm(
        "AAAA11",
        preco_final=100.0,
        proventos=[
            {"data_ex": (hoje - timedelta(days=30)).isoformat(), "valor": 6.0},
            {"data_ex": (hoje - timedelta(days=400)).isoformat(), "valor": 2.0},
        ],
    )

    item = rankings.gerar_rankings()["fiis_ltm"]["itens"][0]

    assert item["dados"]["proventos_12m"] == pytest.approx(6.0)
    assert item["dados"]["proventos_12m_pct"] == pytest.approx(6.0)
    assert item["dados"]["retorno_total_12m_pct"] == pytest.approx(6.0)


def test_magic_formula_leva_ev_ebit_para_o_item(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo_magic("AAAA3", ebit=10.0, ev=50.0, capital=50.0)

    item = rankings.gerar_rankings()["magic_formula"]["itens"][0]

    assert item["dados"]["earnings_yield"] == pytest.approx(0.2)
    assert item["dados"]["ev_ebit"] == pytest.approx(1 / 0.2)
    assert item["dados"]["ebit_ev_pct"] == pytest.approx(20.0)


def test_earnings_yield_no_ranking_ordena_e_ignora_sem_ev(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "AAAA3",
        preco=1.0,
        campos_extra={
            "ebit_12m": 100.0,
            "nro_acoes": 500.0,
            "divida_bruta": 0.0,
            "caixa": 0.0,
        },
    )
    _inserir_ativo(
        "BBBB3",
        preco=1.0,
        campos_extra={
            "ebit_12m": 100.0,
            "nro_acoes": 1000.0,
            "divida_bruta": 0.0,
            "caixa": 0.0,
        },
    )

    dados = rankings.gerar_rankings()["earnings_yield"]

    assert [item["ticker"] for item in dados["itens"]] == ["AAAA3", "BBBB3"]
    assert dados["itens"][0]["pontuacao"] == pytest.approx(20.0)
    assert dados["itens"][1]["pontuacao"] == pytest.approx(10.0)


def test_estrutura_ativa_sem_magic_formula_duplicada(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo("AAAA3")
    _inserir_ativo(
        "BBBB3", campos_extra={"ev_ebit": 6.0, "roic": 18.0, "ebit_12m": 100.0}
    )

    resultado = rankings.gerar_rankings()

    assert "magic_formula_ev_ebit" not in rankings.ORDEM
    assert "magic_formula_ev_ebit" not in resultado
    assert len(resultado) == 13
    assert rankings.estrategias_ativas() == rankings.ORDEM

    indice_magic = rankings.ORDEM.index("magic_formula")
    assert rankings.ORDEM[indice_magic + 1] == "earnings_yield"
    assert rankings.ORDEM[indice_magic + 2] == "upside_consenso"
    assert rankings.ORDEM[indice_magic + 3] == "pl_roe"

    for estrategia in ("earnings_yield", "pl_roe"):
        posicoes = [item["posicao"] for item in resultado[estrategia]["itens"]]
        assert posicoes == list(range(1, len(posicoes) + 1)), estrategia

    assert rankings.classes_da_estrategia("earnings_yield") == {"acao_b3"}
    assert rankings.classes_da_estrategia("magic_formula_ev_ebit") == {"acao_b3"}
    assert rankings.classes_da_estrategia("pl_roe") == {"acao_b3"}


def test_pl_roe_ordena_por_soma_de_postos_e_aceita_banco(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "AAAA3", setor="Bancos", campos_extra={"pl": 6.0, "roe": 22.0}
    )
    _inserir_ativo(
        "BBBB3", setor="Bancos", campos_extra={"pl": 9.0, "roe": 18.0}
    )

    itens = rankings.gerar_rankings()["pl_roe"]["itens"]
    por_ticker = {item["ticker"]: item for item in itens}

    assert [item["ticker"] for item in itens[:2]] == ["AAAA3", "BBBB3"]
    assert por_ticker["AAAA3"]["dados"]["posto_pl"] == 1
    assert por_ticker["AAAA3"]["dados"]["posto_roe"] == 1
    assert por_ticker["AAAA3"]["dados"]["soma_postos"] == 2
    assert por_ticker["AAAA3"]["pontuacao"] == 2
    assert por_ticker["BBBB3"]["dados"]["soma_postos"] == 4
    assert por_ticker["BBBB3"]["pontuacao"] == 4
    assert por_ticker["AAAA3"]["dados"]["pl_x_roe"] == pytest.approx(1.32)
    assert por_ticker["AAAA3"]["motivos"]["setor"] == (
        "instituição financeira: avaliação por P/L e ROE"
    )


def test_earnings_yield_exclui_pssa3_e_mantem_petr4(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "PSSA3",
        preco=1.0,
        setor="Previdência e Seguros",
        campos_extra={"ebit_12m": 100.0, "nro_acoes": 100.0},
    )
    _inserir_ativo(
        "PETR4",
        preco=1.0,
        setor="Petróleo, Gás e Biocombustíveis",
        campos_extra={"ebit_12m": 100.0, "nro_acoes": 100.0},
    )

    dados = rankings.gerar_rankings()["earnings_yield"]
    tickers = [item["ticker"] for item in dados["itens"]]

    assert "PETR4" in tickers
    assert "PSSA3" not in tickers
    fora = {item["ticker"]: item for item in dados["fora_da_lista"]}
    assert "P/L × ROE" in fora["PSSA3"]["motivo"]


def _trend_base(fechamento: list[float]) -> dict:
    datas = pd.bdate_range("2024-01-01", periods=len(fechamento))
    valores = np.array(fechamento, dtype=float)
    df = pd.DataFrame(
        {
            "data": datas,
            "abertura": valores,
            "maxima": valores * 1.01,
            "minima": valores * 0.99,
            "fechamento": valores,
            "volume": 1_000_000.0,
        }
    )
    return {"cotacoes_df": df, "data_ref": "2026-10-28"}


def test_trend_template_aceita_6_de_7_com_motivo_falta():
    serie = list(np.linspace(100.0, 250.0, 300))
    serie[-1] = 212.5
    resultado = rankings._avaliar_trend(_trend_base(serie))

    assert resultado["elegivel"] is True
    assert resultado["dados"]["criterios_atendidos"] == 6
    assert resultado["dados"]["quase_todos"] is True
    assert resultado["dados"]["motivo_falta"]
    assert "preco_acima_sma50" in resultado["dados"]["motivo_falta"]


def test_trend_template_reprova_5_de_7():
    serie = list(np.linspace(100.0, 250.0, 300))
    serie[-1] = 190.0
    resultado = rankings._avaliar_trend(_trend_base(serie))

    assert resultado["dados"]["criterios_atendidos"] == 5
    assert resultado["elegivel"] is False


def test_trend_template_serie_curta_com_todos_disponiveis_e_elegivel():
    serie = list(np.linspace(100.0, 150.0, 160))
    resultado = rankings._avaliar_trend(_trend_base(serie))

    assert resultado["dados"]["criterios_disponiveis"] < 7
    assert resultado["dados"]["criterios_atendidos"] == resultado["dados"][
        "criterios_disponiveis"
    ]
    assert resultado["elegivel"] is True


def test_upside_consenso_exige_cobertura_e_alvo_da_mesma_classe(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "ALFA3",
        preco=100.0,
        campos_extra={
            "alvo_medio": 130.0,
            "consenso_total": 6,
            "consenso_compra": 4,
            "consenso_neutro": 2,
            "consenso_venda": 0,
        },
    )
    _inserir_ativo(
        "BETA3",
        preco=100.0,
        campos_extra={
            "alvo_medio": 180.0,
            "consenso_total": 2,
            "consenso_compra": 2,
            "consenso_neutro": 0,
            "consenso_venda": 0,
        },
    )
    _inserir_ativo(
        "GAMA3",
        preco=100.0,
        campos_extra={
            "alvo_medio": 700.0,
            "consenso_total": 5,
            "consenso_compra": 5,
            "consenso_neutro": 0,
            "consenso_venda": 0,
        },
    )
    _inserir_ativo("DELTA3", preco=100.0)

    dados = rankings.gerar_rankings()["upside_consenso"]

    assert [item["ticker"] for item in dados["itens"]] == ["ALFA3"]
    assert dados["itens"][0]["pontuacao"] == pytest.approx(30.0)
    assert dados["itens"][0]["dados"]["alvo_medio"] == pytest.approx(130.0)
    assert dados["itens"][0]["dados"]["consenso_total"] == 6

    motivos = {item["ticker"]: item["motivo"] for item in dados["fora_da_lista"]}
    assert motivos["BETA3"] == "cobertura de 2 analista(s): mínimo 3"
    assert motivos["GAMA3"] == (
        "alvo provavelmente de outra classe do papel (unit × PN/ON)"
    )
    assert motivos["DELTA3"] == "sem consenso na fonte"


def test_upside_consenso_ordena_por_upside_decrescente(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ativo(
        "AAAA3",
        preco=100.0,
        campos_extra={
            "alvo_medio": 130.0,
            "consenso_total": 4,
            "consenso_compra": 3,
            "consenso_neutro": 1,
            "consenso_venda": 0,
        },
    )
    _inserir_ativo(
        "BBBB3",
        preco=100.0,
        campos_extra={
            "alvo_medio": 112.0,
            "consenso_total": 4,
            "consenso_compra": 2,
            "consenso_neutro": 2,
            "consenso_venda": 0,
        },
    )

    itens = rankings.gerar_rankings()["upside_consenso"]["itens"]

    assert [item["ticker"] for item in itens] == ["AAAA3", "BBBB3"]
    assert [item["pontuacao"] for item in itens] == pytest.approx([30.0, 12.0])


def test_upside_consenso_classe_e_estrategia_ativa():
    assert rankings.classes_da_estrategia("upside_consenso") == {"acao_b3"}
    assert "upside_consenso" in rankings.estrategias_ativas()


def test_principais_completam_campo_nulo_com_o_valor_da_estrategia():
    base = {"ticker": "CDII11", "dy": None, "pvp": 0.91}

    indicadores = rankings._principais(
        base, "fiis", {"dy": 15.99, "pvp": 0.5}
    )

    assert indicadores["dy"] == 15.99
    # valor do ativo continua tendo prioridade
    assert indicadores["pvp"] == 0.91


def _proventos_nos_meses(meses: list[int]) -> list[dict]:
    """Proventos nos meses pedidos, no ano passado e neste (até o mês atual)."""

    hoje = date.today()
    itens = []
    for ano in (hoje.year - 1, hoje.year):
        for mes in meses:
            if ano == hoje.year and mes > hoje.month:
                continue
            itens.append({"data_ex": date(ano, mes, 1).isoformat(), "valor": 1.0})
    return itens


def test_regularidade_proventos_ordena_por_meses_e_observa_recorrencia(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    hoje = date.today()
    # Meses sempre presentes nos DOIS anos da janela: o corrente e os anteriores.
    meses_aaaa = [(hoje.month - 1 - i) % 12 + 1 for i in range(2)]
    meses_bbbb = [(hoje.month - 1 - i) % 12 + 1 for i in range(4)]
    _inserir_ativo("AAAA3", proventos=_proventos_nos_meses(meses_aaaa))
    _inserir_ativo("BBBB3", proventos=_proventos_nos_meses(meses_bbbb))
    _inserir_ativo(
        "CCCC3",
        proventos=[
            {"data_ex": date(hoje.year, hoje.month, 1).isoformat(), "valor": 1.0}
        ],
    )
    _inserir_ativo("DDDD3")

    dados = rankings.gerar_rankings()["regularidade_proventos"]

    assert [item["ticker"] for item in dados["itens"]] == [
        "BBBB3",
        "AAAA3",
        "CCCC3",
    ]
    assert [item["posicao"] for item in dados["itens"]] == [1, 2, 3]
    por_ticker = {item["ticker"]: item for item in dados["itens"]}
    assert por_ticker["AAAA3"]["dados"]["regularidade_24m_meses"] == 4
    assert por_ticker["AAAA3"]["dados"]["regularidade_24m_pct"] == round(
        4 / 24 * 100, 1
    )
    assert por_ticker["AAAA3"]["dados"]["proventos_24m_qtd"] == 4
    assert por_ticker["AAAA3"]["dados"]["proventos_24m_observacao"] == (
        "paga geralmente em "
        + ", ".join(ind.MESES_CURTOS[mes - 1] for mes in sorted(meses_aaaa))
    )
    assert por_ticker["BBBB3"]["dados"]["regularidade_24m_meses"] == 8
    assert por_ticker["BBBB3"]["dados"]["proventos_24m_observacao"] == (
        "paga geralmente em "
        + ", ".join(ind.MESES_CURTOS[mes - 1] for mes in sorted(meses_bbbb))
    )
    assert por_ticker["CCCC3"]["dados"]["regularidade_24m_meses"] == 1

    fora = {item["ticker"]: item for item in dados["fora_da_lista"]}
    assert fora["DDDD3"]["motivo"] == "sem provento nos últimos 24 meses"
