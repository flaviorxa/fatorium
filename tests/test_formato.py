from datetime import datetime, timezone

from app.web import formato


def test_moeda_brasileira():
    assert formato.moeda(1234.56) == "R$ 1.234,56"
    assert formato.moeda(0) == "R$ 0,00"
    assert formato.moeda(None) is None


def test_percentual_com_virgula_e_sinal():
    assert formato.percentual(12.345) == "12,35%"
    assert formato.percentual(-1.2, sinal=True) == "-1,20%"
    assert formato.percentual(1.2, sinal=True) == "+1,20%"
    assert formato.percentual(None) is None


def test_numero_e_inteiro():
    assert formato.numero(1234567.891, casas=2) == "1.234.567,89"
    assert formato.inteiro(1234.9) == "1.235"
    assert formato.numero("10,5") == "10,50"
    assert formato.numero(float("nan")) is None


def test_data_br():
    assert formato.data_br("2026-10-28") == "28/10/2026"
    assert formato.data_br("2026-10-28T10:00:00") == "28/10/2026"
    assert formato.data_br(None) is None


def test_hora_br_converte_utc_para_brasilia():
    assert formato.hora_br("2026-10-29T16:53:42+00:00") == "13:53"
    assert formato.hora_br("2026-10-29T16:53:42Z") == "13:53"


def test_data_br_converte_virada_de_dia():
    assert formato.data_br("2026-10-30T01:30:00+00:00") == "29/10/2026"
    assert formato.hora_br("2026-10-30T01:30:00+00:00") == "22:30"


def test_data_pura_permanece_intacta():
    assert formato.data_br("2026-10-28") == "28/10/2026"


def test_datetime_com_tzinfo_converte():
    instante = datetime(2026, 10, 29, 16, 53, 42, tzinfo=timezone.utc)
    assert formato.hora_br(instante) == "13:53"
    assert formato.data_br(instante) == "29/10/2026"

    naive = datetime(2026, 10, 29, 16, 53, 42)
    assert formato.hora_br(naive) == "16:53"
    assert formato.data_br(naive) == "29/10/2026"


def test_celula_na_com_motivo():
    celula = formato.celula(None, "numero", motivo="LPA ausente")
    assert celula["texto"] == "N/A"
    assert celula["motivo"] == "LPA ausente"

    celula = formato.celula(10.0, "moeda")
    assert celula["texto"] == "R$ 10,00"
    assert celula["motivo"] is None
