import numpy as np
import pandas as pd
import pytest

from app.motor import tecnicos


def _serie_alta(dias: int = 300) -> pd.DataFrame:
    indice = pd.bdate_range("2024-01-01", periods=dias)
    fechamento = np.linspace(100.0, 250.0, dias)
    return pd.DataFrame(
        {
            "data": indice,
            "abertura": fechamento,
            "maxima": fechamento * 1.01,
            "minima": fechamento * 0.99,
            "fechamento": fechamento,
            "volume": 1000.0,
        }
    )


def test_trend_template_serie_longa_atende_todos():
    template = tecnicos.trend_template(_serie_alta(300))
    criterios = [valor for valor in template.values() if isinstance(valor, dict)]

    assert len(criterios) == 7
    assert all(criterio["atende"] is True for criterio in criterios)
    assert template["atende_todos"] is True
    assert template["criterios_disponiveis"] == 7


def test_trend_template_serie_curta_indisponivel():
    template = tecnicos.trend_template(_serie_alta(40))
    criterios = [valor for valor in template.values() if isinstance(valor, dict)]

    assert all(criterio["atende"] is None for criterio in criterios)
    assert template["atende_todos"] is None
    assert template["criterios_disponiveis"] == 0


def test_resumo_semanal_fecha_e_soma_volume():
    datas = pd.bdate_range("2026-01-05", periods=5)
    df = pd.DataFrame(
        {
            "data": datas,
            "abertura": [10.0, 11.0, 12.0, 13.0, 14.0],
            "maxima": [11.0, 12.0, 13.0, 14.0, 15.0],
            "minima": [9.0, 10.0, 11.0, 12.0, 13.0],
            "fechamento": [10.0, 11.0, 12.0, 13.0, 14.0],
            "volume": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    )
    semanal = tecnicos.resumo_semanal(df)

    assert len(semanal) == 1
    linha = semanal.iloc[0]
    assert linha["abertura"] == 10.0
    assert linha["maxima"] == 15.0
    assert linha["minima"] == 9.0
    assert linha["fechamento"] == 14.0
    assert linha["volume"] == 150.0
    assert bool(linha["completa"]) is True


def test_sma_semanal_usa_nove_semanas_nao_nove_dias():
    semanas = []
    for semana in range(1, 10):
        for _ in range(5):
            semanas.append(float(semana * 10))

    df = pd.DataFrame(
        {
            "data": pd.bdate_range("2026-01-05", periods=45),
            "abertura": semanas,
            "maxima": semanas,
            "minima": semanas,
            "fechamento": semanas,
            "volume": 1.0,
        }
    )
    medias = tecnicos.sma_semanal(df, janelas=(9,))

    assert medias["sma9s"].iloc[:8].isna().all()
    assert medias["sma9s"].iloc[8] == pytest.approx(50.0)
    assert medias["sma9s"].iloc[8] == pytest.approx(df["fechamento"].iloc[:45].mean())


def test_forca_relativa_alinha_por_data():
    ativo_datas = pd.bdate_range("2025-01-01", periods=300)
    bench_datas = ativo_datas[3:]
    ativo = pd.DataFrame(
        {"data": ativo_datas, "fechamento": np.linspace(10, 20, len(ativo_datas))}
    )
    benchmark = pd.DataFrame(
        {"data": bench_datas, "fechamento": np.linspace(100, 110, len(bench_datas))}
    )

    resultado = tecnicos.forca_relativa(ativo, benchmark)

    assert resultado["rs_1m"] is not None
    assert resultado["rs_1m"] == pytest.approx(
        resultado["ativo_1m"] - resultado["benchmark_1m"]
    )


def test_forca_relativa_12m_com_300_dias():
    datas = pd.bdate_range("2024-01-01", periods=300)
    inicio = datas[-1] - pd.DateOffset(months=12)
    posicao = int(datas.searchsorted(inicio, side="right")) - 1

    valores_ativo = np.full(len(datas), 100.0)
    valores_ativo[posicao:] = np.linspace(100.0, 120.0, len(datas) - posicao)
    valores_bench = np.full(len(datas), 100.0)
    valores_bench[posicao:] = np.linspace(100.0, 110.0, len(datas) - posicao)

    ativo = pd.DataFrame({"data": datas, "fechamento": valores_ativo})
    benchmark = pd.DataFrame({"data": datas, "fechamento": valores_bench})

    resultado = tecnicos.forca_relativa(ativo, benchmark)

    assert resultado["ativo_12m"] == pytest.approx(20.0)
    assert resultado["benchmark_12m"] == pytest.approx(10.0)
    assert resultado["rs_12m"] == pytest.approx(10.0)


def test_forca_relativa_exige_benchmark_com_260_barras():
    datas = pd.bdate_range("2024-01-01", periods=300)
    ativo = pd.DataFrame({"data": datas, "fechamento": np.linspace(10, 20, 300)})
    benchmark = pd.DataFrame(
        {"data": datas[-100:], "fechamento": np.linspace(100, 110, 100)}
    )

    with pytest.raises(ValueError, match="260"):
        tecnicos.forca_relativa(ativo, benchmark)


def test_sinais_sem_historico_devolvem_none():
    sinais = tecnicos.sinais(_serie_alta(30))
    por_nome = {item["sinal"]: item for item in sinais}

    assert por_nome["tendencia_alta"]["atende"] is None
    assert por_nome["tendencia_alta"]["motivo"] == "histórico insuficiente"
    assert por_nome["proximo_maxima"]["atende"] is None


def test_bollinger_usa_sma20_e_dois_desvios_populacionais():
    fechamento = pd.Series(np.arange(1.0, 21.0), index=pd.bdate_range("2025-01-01", periods=20))
    bandas = tecnicos.bollinger(fechamento, janela=20, desvios=2.0)

    assert bandas["media"].iloc[:19].isna().all()
    media = float(bandas["media"].iloc[-1])
    superior = float(bandas["superior"].iloc[-1])
    inferior = float(bandas["inferior"].iloc[-1])

    assert media == pytest.approx(10.5)
    largura = superior - media
    assert largura == pytest.approx(media - inferior)
    assert largura == pytest.approx(2.0 * float(np.std(np.arange(1.0, 21.0))))


def test_bollinger_serie_curta_nao_inventa_banda():
    bandas = tecnicos.bollinger(pd.Series([10.0, 11.0, 12.0]), janela=20)

    assert bandas[["media", "superior", "inferior"]].isna().all().all()
