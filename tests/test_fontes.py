from datetime import date, timedelta

import pandas as pd
import pytest

from app.fontes import fundamentus_br, yahoo
from app.fontes.base import com_prazo, com_retry, validar_cotacoes


def test_normalizar_ticker():
    assert yahoo.normalizar_ticker("PETR4") == "PETR4.SA"
    assert yahoo.normalizar_ticker("VALE3") == "VALE3.SA"
    assert yahoo.normalizar_ticker("BTC-USD") == "BTC-USD"
    assert yahoo.normalizar_ticker("^BVSP") == "^BVSP"
    assert yahoo.normalizar_ticker("USDBRL=X") == "USDBRL=X"
    assert yahoo.normalizar_ticker("BZ=F") == "BZ=F"


def test_validar_cotacoes_descarta_invalidas():
    hoje = date.today()
    df = pd.DataFrame(
        [
            {
                "data": hoje - timedelta(days=1),
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.0,
                "fechamento": 10.5,
                "fechamento_ajustado": 10.5,
                "volume": 100.0,
            },
            {
                "data": hoje - timedelta(days=2),
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.0,
                "fechamento": None,
                "fechamento_ajustado": 10.5,
                "volume": 100.0,
            },
            {
                "data": hoje - timedelta(days=3),
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.0,
                "fechamento": -1.0,
                "fechamento_ajustado": 10.5,
                "volume": 100.0,
            },
            {
                "data": hoje + timedelta(days=1),
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.0,
                "fechamento": 10.5,
                "fechamento_ajustado": 10.5,
                "volume": 100.0,
            },
            {
                "data": "ontem",
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.0,
                "fechamento": 10.5,
                "fechamento_ajustado": 10.5,
                "volume": 100.0,
            },
        ]
    )
    limpo, avisos = validar_cotacoes(df)
    assert len(limpo) == 1
    assert limpo.iloc[0]["data"] == (hoje - timedelta(days=1)).isoformat()
    assert len(avisos) >= 3


def test_conversao_numeros_br():
    assert fundamentus_br.para_numero("7,30%") == 7.3
    assert fundamentus_br.para_numero("1.234.567,89") == 1234567.89
    assert fundamentus_br.para_numero("1.234.567") == 1234567.0
    assert fundamentus_br.para_numero("52.51") == 52.51
    assert fundamentus_br.para_numero("-") is None
    assert fundamentus_br.para_numero("") is None
    assert fundamentus_br.para_numero(None) is None
    assert fundamentus_br.para_numero(5.07) == 5.07


def test_papel_offline(monkeypatch):
    dados = {
        "Cotacao": "52,51",
        "PL": "5,07",
        "PVP": "1,41",
        "Div_Yield": "6,9%",
        "EV_EBITDA": "3,07",
        "EV_EBIT": "4,22",
        "ROE": "27,7%",
        "ROIC": "19,7%",
        "Marg_Bruta": "50,6%",
        "Marg_EBIT": "42,8%",
        "Marg_Liquida": "24,4%",
        "LPA": "10,35",
        "Div_Liquida": "312.769.000.000,00",
        "Div_Bruta": "546.000.000.000,00",
        "Disponibilidades": "23.000.000.000,00",
        "Cres_Rec_5a": "8,5%",
        "Div_Liq_Patrim": "0,65",
        "Patrim_Liq": "480.950.000.000,00",
        "Receita_Liquida_12m": "548.493.000.000,00",
        "Lucro_Liquido_12m": "133.376.000.000,00",
        "EBIT_12m": "234.531.000.000,00",
        "Nro_Acoes": "12.888.700.000",
        "Liquidez_Corr": "0,85",
        "Vol_med_2m": "2.538.370.000",
        "Max_52_sem": "59,68",
        "Min_52_sem": "27,46",
        "Empresa": "PETROBRAS PN",
        "Setor": "Petróleo, Gás e Biocombustíveis",
        "Subsetor": "Petróleo e Gás",
        "Data_ult_cot": "2026-10-28",
    }
    monkeypatch.setattr(
        fundamentus_br.fundamentus,
        "get_papel",
        lambda ticker: pd.DataFrame([dados]),
    )

    campos, avisos = fundamentus_br.papel("PETR4", "acao_b3")

    assert campos["preco"] == 52.51
    assert campos["pl"] == 5.07
    assert campos["pvp"] == 1.41
    assert campos["dy"] == 6.9
    assert campos["roe"] == 27.7
    assert campos["lpa"] == 10.35
    assert campos["patrimonio_liquido"] == 480950000000.0
    assert campos["nro_acoes"] == 12888700000.0
    assert campos["vpa_calculado"] == pytest.approx(480950000000.0 / 12888700000.0)
    assert campos["data_cotacao"] == "2026-10-28"
    assert campos["empresa"] == "PETROBRAS PN"
    assert campos["setor"] == "Petróleo, Gás e Biocombustíveis"
    assert campos["subsetor"] == "Petróleo e Gás"
    assert avisos == []


def test_papel_rejeita_classe_nao_coberta():
    with pytest.raises(ValueError, match="fonte não cobre cripto"):
        fundamentus_br.papel("BTC-USD", "cripto")


def test_papel_fii_offline(monkeypatch):
    tabela0 = pd.DataFrame(
        [
            ["?FII", "KNIP11", "?Cotação", "86,90"],
            ["?Nome", "KINEA ÍNDICES DE PREÇOS FII", "?Data últ cot", "28/10/2026"],
            ["?Mandato", None, "?Min 52 sem", "76,78"],
            ["?Segmento", None, "?Max 52 sem", "90,00"],
        ]
    )
    tabela1 = pd.DataFrame(
        [["?Valor de mercado", "6.958.790.000", "?Nro. Cotas", "80.078.186"]]
    )
    tabela2 = pd.DataFrame(
        [
            ["Dia", "-1,14%", "?FFO Yield", "14,28%", "?FFO/Cota", "12.41"],
            ["Mês", "-1,92%", "?Div. Yield", "11,5%", "?Dividendo/cota", "10.11"],
        ]
    )
    monkeypatch.setattr(fundamentus_br.fundamentus, "get_papel", lambda ticker: None)
    monkeypatch.setattr(
        fundamentus_br.fundamentus,
        "get_detalhes_raw",
        lambda ticker: [tabela0, tabela1, tabela2],
    )

    campos, avisos = fundamentus_br.papel("KNIP11", "fii")

    assert campos["preco"] == 86.9
    assert campos["dy"] == 11.5
    assert campos["nro_acoes"] == 80078186.0
    assert campos["data_cotacao"] == "2026-10-28"
    assert campos["empresa"].startswith("KINEA")
    assert "vpa_calculado" not in campos
    assert any("FII" in aviso for aviso in avisos)


class _TickerInfo:
    def __init__(self, info):
        self.info = info


def test_fundamentos_basicos_le_info_uma_vez(monkeypatch):
    chamadas = []

    class Ticker:
        def __init__(self, simbolo):
            chamadas.append(simbolo)
            self.info = {
                "priceToBook": 0.91,
                "bookValue": 95.11,
                "marketCap": 6_958_790_000,
                "longName": "KINEA ÍNDICES DE PREÇOS FII",
                "currency": "BRL",
                "setor": "ignorado",
            }

    monkeypatch.setattr(yahoo.yf, "Ticker", Ticker)

    dados = yahoo.fundamentos_basicos("KNIP11")

    assert chamadas == ["KNIP11.SA"]
    assert dados == {
        "price_to_book": 0.91,
        "book_value": 95.11,
        "market_cap": 6_958_790_000.0,
        "nome_longo": "KINEA ÍNDICES DE PREÇOS FII",
        "moeda": "BRL",
    }


def test_fundamentos_basicos_tolera_falha(monkeypatch):
    class Ticker:
        def __init__(self, simbolo):
            raise RuntimeError("sem rede")

    monkeypatch.setattr(yahoo.yf, "Ticker", Ticker)

    dados = yahoo.fundamentos_basicos("KNIP11")

    assert dados == {
        "price_to_book": None,
        "book_value": None,
        "market_cap": None,
        "nome_longo": None,
        "moeda": None,
    }


def test_fundamentos_basicos_campos_ausentes_viram_none(monkeypatch):
    monkeypatch.setattr(
        yahoo.yf, "Ticker", lambda simbolo: _TickerInfo({"priceToBook": None})
    )

    dados = yahoo.fundamentos_basicos("KNIP11")

    assert dados["price_to_book"] is None
    assert dados["book_value"] is None


def test_com_retry_backoff(monkeypatch):
    esperas = []
    chamadas = []
    monkeypatch.setattr("app.fontes.base.time.sleep", lambda segundos: esperas.append(segundos))

    def falhar():
        chamadas.append(1)
        raise ValueError("falhou")

    with pytest.raises(ValueError):
        com_retry(falhar, tentativas=3, espera=1.0)

    assert len(chamadas) == 4
    assert esperas == [1.0, 2.0, 4.0]


def test_com_prazo_estoura_em_vez_de_pendurar():
    import time

    with pytest.raises(TimeoutError):
        com_prazo(lambda: time.sleep(1.0), segundos=0.05)
