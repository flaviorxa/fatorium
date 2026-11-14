import numpy as np
import pandas as pd
import pytest

from app.motor import screening
from app.motor.estrategias import CONFIG_PADRAO


def _serie(valores, inicio="2024-01-01") -> pd.Series:
    return pd.Series(
        np.asarray(valores, dtype=float),
        index=pd.bdate_range(inicio, periods=len(valores)),
    )


def _df(valores, inicio="2024-01-01") -> pd.DataFrame:
    fechamento = np.asarray(valores, dtype=float)
    return pd.DataFrame(
        {
            "data": pd.bdate_range(inicio, periods=len(fechamento)),
            "abertura": fechamento,
            "maxima": fechamento * 1.01,
            "minima": fechamento * 0.99,
            "fechamento": fechamento,
            "volume": 1_000.0,
        }
    )


def _ultimo_cruzamento(serie: pd.Series, lado: str) -> int:
    diferenca = screening.ema(serie, 9) - screening.ema(serie, 21)
    encontrados = []
    for posicao in range(1, len(diferenca)):
        anterior = diferenca.iloc[posicao - 1]
        atual = diferenca.iloc[posicao]
        if pd.isna(anterior) or pd.isna(atual):
            continue
        if lado == "alta" and anterior <= 0 < atual:
            encontrados.append(posicao)
        if lado == "baixa" and anterior >= 0 > atual:
            encontrados.append(posicao)
    return encontrados[-1]


def test_serie_sobe_suave_e_acelera_detecta_cruzamento_de_alta():
    valores = np.concatenate(
        [np.linspace(110.0, 100.0, 90), np.linspace(101.0, 145.0, 30)]
    )
    serie = _serie(valores)

    resultado = screening.cruzamentos(serie, janela_dias=30)

    posicao = _ultimo_cruzamento(serie, "alta")
    assert resultado["alta"] is not None
    assert resultado["baixa"] is None
    assert resultado["dias_desde"] == len(serie) - 1 - posicao


def test_serie_inverte_detecta_cruzamento_de_baixa():
    valores = np.concatenate(
        [np.linspace(100.0, 145.0, 90), np.linspace(144.0, 95.0, 30)]
    )
    serie = _serie(valores)

    resultado = screening.cruzamentos(serie, janela_dias=30)

    posicao = _ultimo_cruzamento(serie, "baixa")
    assert resultado["baixa"] is not None
    assert resultado["alta"] is None
    assert resultado["dias_desde"] == len(serie) - 1 - posicao


def test_cruzamento_fora_da_janela_vira_none():
    valores = np.concatenate(
        [np.linspace(110.0, 100.0, 80), np.linspace(101.0, 140.0, 20)]
    )
    serie = _serie(valores)
    posicao = _ultimo_cruzamento(serie, "alta")
    recortada = serie.iloc[: posicao + 11]

    fora = screening.cruzamentos(recortada, janela_dias=3)
    dentro = screening.cruzamentos(recortada, janela_dias=10)

    assert fora == {"alta": None, "baixa": None, "dias_desde": None}
    assert dentro["alta"] is not None
    assert dentro["dias_desde"] == 10


def test_alinhamento_de_alta_exige_as_duas_medias_subindo():
    serie = _serie(np.linspace(100.0, 200.0, 120))
    m9 = screening.ema(serie, 9)
    m21 = screening.ema(serie, 21)

    resultado = screening.alinhamento(
        float(serie.iloc[-1]), float(m9.iloc[-1]), float(m21.iloc[-1]), m9, m21
    )
    assert resultado["alta_forte"] is True
    assert resultado["m9_subindo"] is True
    assert resultado["m21_subindo"] is True

    m9_caindo = _serie(
        np.concatenate([np.full(9, float(m9.iloc[-1]) * 1.1), [float(m9.iloc[-1])]])
    )
    parcial = screening.alinhamento(
        float(serie.iloc[-1]), float(m9.iloc[-1]), float(m21.iloc[-1]), m9_caindo, m21
    )
    assert parcial["alta_forte"] is False
    assert parcial["m9_subindo"] is False


def test_pullback_toca_a_m21_e_fecha_acima():
    df = _df(np.linspace(100.0, 130.0, 100))
    df.loc[df.index[-1], ["abertura", "minima", "maxima", "fechamento"]] = [
        110.8,
        110.4,
        112.0,
        111.5,
    ]

    resultado = screening.pullback(df, m9=120.0, m21=110.0)

    assert resultado is not None
    assert resultado["media_tocada"] == "m21"
    assert resultado["candle_reversao"] is True
    assert resultado["data"] == df.iloc[-1]["data"].date()


def test_pullback_que_fecha_abaixo_da_media_nao_e_detectado():
    df = _df(np.linspace(100.0, 130.0, 100))
    df.loc[df.index[-1], ["abertura", "minima", "maxima", "fechamento"]] = [
        105.0,
        99.0,
        106.0,
        100.0,
    ]

    assert screening.pullback(df, m9=120.0, m21=110.0) is None


def test_esticado_respeita_o_limiar_do_preco():
    acima = screening.esticado(112.0, 100.0, 98.0)
    perto = screening.esticado(105.0, 100.0, 98.0)

    assert acima["dist_preco_m9_pct"] == pytest.approx(12.0)
    assert acima["esticado"] is True
    assert perto["dist_preco_m9_pct"] == pytest.approx(5.0)
    assert perto["esticado"] is False


def test_historico_curto_fica_indisponivel_e_fora_das_listas(monkeypatch):
    df = _df(np.linspace(100.0, 110.0, 40))
    avaliado = screening.avaliar_ativo(
        "CURT3", "acao_b3", df, CONFIG_PADRAO["screening"]
    )

    assert avaliado["disponivel"] is False
    assert "histórico insuficiente" in avaliado["motivos"][0]

    monkeypatch.setattr(
        screening, "_carregar_ativos", lambda classe=None: [("CURT3", "acao_b3", df)]
    )
    screening.limpar_cache()
    try:
        listas = screening.triar(CONFIG_PADRAO["screening"])
        assert all(not itens for itens in listas.values())
    finally:
        screening.limpar_cache()
