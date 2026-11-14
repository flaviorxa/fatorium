import csv
from datetime import date

import pytest

from app.fontes import cvm
from app.motor import estrategias
from app.motor.indicadores import avaliar_fundamentalista

GERAL = [
    "CNPJ_Fundo_Classe",
    "Data_Referencia",
    "Versao",
    "Nome_Fundo_Classe",
    "Segmento_Atuacao",
    "Codigo_ISIN",
]
COMPLEMENTO = [
    "CNPJ_Fundo_Classe",
    "Data_Referencia",
    "Versao",
    "Total_Numero_Cotistas",
    "Patrimonio_Liquido",
    "Cotas_Emitidas",
    "Valor_Patrimonial_Cotas",
    "Percentual_Despesas_Taxa_Administracao",
]


def _escrever(caminho, cabecalho, linhas):
    with caminho.open("w", encoding="latin-1", newline="") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        escritor.writerow(cabecalho)
        escritor.writerows(linhas)


def _fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(cvm, "DIRETORIO", tmp_path)
    _escrever(
        tmp_path / "inf_mensal_fii_geral_2026.csv",
        GERAL,
        [
            [
                "11.111.111/0001-11",
                "2026-01-31",
                "1",
                "FUNDO ANTIGO AÇÃO",
                "Logística",
                "BRAAAACTA001",
            ],
            [
                "11.111.111/0001-11",
                "2026-03-31",
                "1",
                "FUNDO NOVO AÇÃO",
                "Shoppings",
                "BRAAAACTA001",
            ],
            [
                "22.222.222/0001-22",
                "2026-03-31",
                "2",
                "OUTRO FUNDO",
                "Papel",
                "BRBBBBCTF002",
            ],
        ],
    )
    _escrever(
        tmp_path / "inf_mensal_fii_complemento_2026.csv",
        COMPLEMENTO,
        [
            [
                "11.111.111/0001-11",
                "2026-01-31",
                "1",
                "100",
                "1000",
                "1000",
                "100.0",
                "0.012",
            ],
            [
                "11.111.111/0001-11",
                "2026-03-31",
                "1",
                "120",
                "12000",
                "",
                "120.0",
                "0.012",
            ],
            [
                "22.222.222/0001-22",
                "2026-03-31",
                "2",
                "",
                "5000",
                "500",
                "10.0",
                "1.2",
            ],
        ],
    )
    return cvm.ler_fundos(2026)


def test_le_csv_latin1_e_fica_com_data_mais_recente(monkeypatch, tmp_path):
    fundos = _fixture(monkeypatch, tmp_path)
    fundo = fundos["11.111.111/0001-11"]

    assert fundo["data_ref"] == "2026-03-31"
    assert fundo["nome"] == "FUNDO NOVO AÇÃO"
    assert fundo["segmento"] == "Shoppings"
    assert fundo["vpa"] == 120.0
    assert fundo["cotistas"] == 120.0


def test_campo_vazio_vira_none_e_nunca_zero(monkeypatch, tmp_path):
    fundos = _fixture(monkeypatch, tmp_path)

    assert fundos["11.111.111/0001-11"]["cotas"] is None
    assert fundos["22.222.222/0001-22"]["cotistas"] is None
    assert fundos["11.111.111/0001-11"]["cotas"] != 0


def test_taxa_administracao_guarda_valor_bruto_e_campo_de_origem(
    monkeypatch, tmp_path
):
    fundos = _fixture(monkeypatch, tmp_path)

    primeiro = fundos["11.111.111/0001-11"]
    segundo = fundos["22.222.222/0001-22"]
    assert primeiro["taxa_administracao_informe"] == 0.012
    assert segundo["taxa_administracao_informe"] == 1.2
    assert (
        primeiro["taxa_administracao_campo"]
        == "Percentual_Despesas_Taxa_Administracao"
    )


def test_pvp_da_cvm_usa_preco_sobre_vpa():
    pacote = avaliar_fundamentalista({"preco": 30.0, "vpa_cvm": 25.0})

    assert pacote["pvp"] == 1.2
    assert pacote["pvp_origem"] == "cvm"


def test_pvp_da_cvm_sem_vpa_nao_divid_por_zero():
    pacote = avaliar_fundamentalista({"preco": 30.0, "vpa_cvm": None})
    assert pacote["pvp"] is None
    assert pacote["pvp_origem"] is None

    pacote_zero = avaliar_fundamentalista({"preco": 30.0, "vpa_cvm": 0.0})
    assert pacote_zero["pvp"] is None


def test_pvp_knip11_prioriza_cvm_sobre_yahoo():
    campos = {
        "preco": 86.90,
        "vpa_cvm": 95.1114173822569,
        "pvp_yahoo": 0.80,
    }

    pacote = avaliar_fundamentalista(campos, classe="fii")

    assert pacote["pvp"] == pytest.approx(0.914, abs=0.001)
    assert pacote["pvp_origem"] == "cvm"


def test_pvp_da_acao_prefere_yahoo_a_cvm():
    campos = {
        "preco": 30.0,
        "vpa_cvm": 25.0,
        "pvp_yahoo": 1.1,
    }

    pacote = avaliar_fundamentalista(campos, classe="acao_b3")

    assert pacote["pvp"] == 1.1
    assert pacote["pvp_origem"] == "yahoo"


def test_pvp_fii_usa_yahoo_quando_nao_ha_cvm():
    campos = {"preco": 100.0, "pvp_yahoo": 0.88}

    pacote = avaliar_fundamentalista(campos, classe="fii")

    assert pacote["pvp"] == 0.88
    assert pacote["pvp_origem"] == "yahoo"


def test_pvp_do_fundamentus_tem_prioridade():
    campos = {"preco": 30.0, "pvp": 1.4, "vpa_cvm": 25.0, "pvp_yahoo": 1.1}

    pacote = avaliar_fundamentalista(campos, classe="fii")

    assert pacote["pvp"] == 1.4
    assert pacote["pvp_origem"] == "fundamentus"


def _campos_fii(vpa_cvm=None, liquidez=1_000_000.0):
    campos = {
        "preco": 100.0,
        "dy": 9.0,
        "liquidez_diaria_brl": liquidez,
        "meses_com_provento_24m": 24,
    }
    if vpa_cvm is not None:
        campos["vpa_cvm"] = vpa_cvm
    return campos


def test_fii_mapeado_nao_leva_p_vp_para_indisponiveis():
    campos = _campos_fii(vpa_cvm=110.0)
    base = {**campos, **avaliar_fundamentalista(campos)}

    resultado = estrategias.avaliar_fii(base, estrategias.CONFIG_PADRAO)

    assert resultado["dados"]["pvp"] == base["pvp"]
    assert resultado["dados"]["pvp_origem"] == "cvm"
    assert "p_vp" not in resultado["indisponiveis"]


def test_fii_sem_mapa_mantem_p_vp_indisponivel():
    campos = _campos_fii()
    base = {**campos, **avaliar_fundamentalista(campos)}

    resultado = estrategias.avaliar_fii(base, estrategias.CONFIG_PADRAO)

    assert "p_vp" in resultado["indisponiveis"]


def test_fii_com_liquidez_de_6_mil_passa_do_filtro():
    campos = _campos_fii(liquidez=6_000.0)
    base = {**campos, **avaliar_fundamentalista(campos)}

    resultado = estrategias.avaliar_fii(base, estrategias.CONFIG_PADRAO)

    assert resultado["elegivel"] is True
    assert "liquidez" in resultado["aprovados"]


def test_fii_com_liquidez_de_4_mil_reprova_com_valor_e_unidade():
    campos = _campos_fii(liquidez=4_000.0)
    base = {**campos, **avaliar_fundamentalista(campos)}

    resultado = estrategias.avaliar_fii(base, estrategias.CONFIG_PADRAO)

    assert resultado["elegivel"] is False
    assert (
        resultado["motivos"]["liquidez"]
        == "liquidez R$ 4.000/dia abaixo do mínimo R$ 5.000/dia"
    )


DIARIO = [
    "TP_FUNDO_CLASSE",
    "CNPJ_FUNDO_CLASSE",
    "DT_COMPTC",
    "VL_QUOTA",
    "VL_PATRIM_LIQ",
    "NR_COTST",
]
FIAGRO = ["CNPJ_Classe", "Nome_Classe", "Data_Referencia", "Numero_Cotistas"]
FIAGRO_SUBCLASSE = [
    "CNPJ_Classe",
    "Nome_Classe",
    "Data_Referencia",
    "Numero_Cotas",
    "Valor_Patrimonial_Cota",
]
FIP = [
    "TP_FUNDO_CLASSE",
    "CNPJ_FUNDO_CLASSE",
    "DENOM_SOCIAL",
    "DT_COMPTC",
    "VL_PATRIM_LIQ",
    "VL_QUOTA_CLASSE",
    "QT_COTA_SUBSCR_CLASSE",
    "NR_TOTAL_COTST_SUBSCR",
]


def test_diario_usa_a_cota_patrimonial_mais_recente(monkeypatch, tmp_path):
    monkeypatch.setattr(cvm, "DIRETORIO", tmp_path)
    _escrever(
        tmp_path / "inf_diario_fi_202610.csv",
        DIARIO,
        [
            ["CLASSES - FIF", "42.730.834/0001-00", "2026-10-27", "100.51", "2067825969.89", "91422"],
            ["CLASSES - FIF", "42.730.834/0001-00", "2026-10-28", "100.65", "2070578361.26", "0"],
        ],
    )

    fundo = cvm.ler_diario_fi("202610")["42.730.834/0001-00"]

    assert fundo["vpa"] == 100.65
    assert fundo["patrimonio_liquido"] == 2070578361.26
    assert fundo["data_ref"] == "2026-10-28"
    # NR_COTST 0 no diário é "não informado": não pode virar zero cotista
    assert fundo["cotistas"] is None


def test_fiagro_le_a_cota_da_subclasse_e_formata_o_cnpj(monkeypatch, tmp_path):
    monkeypatch.setattr(cvm, "DIRETORIO", tmp_path)
    _escrever(
        tmp_path / "inf_mensal_fiagro_202609.csv",
        FIAGRO,
        [["48903610000121", "SPARTA FIAGRO", "2026-09-01", "10833"]],
    )
    _escrever(
        tmp_path / "inf_mensal_fiagro_subclasse_202609.csv",
        FIAGRO_SUBCLASSE,
        [["48903610000121", "SPARTA FIAGRO", "2026-09-01", "2369836", "101.31"]],
    )

    fundo = cvm.ler_fiagro("202609")["48.903.610/0001-21"]

    assert fundo["vpa"] == 101.31
    assert fundo["cotas"] == 2369836
    assert fundo["cotistas"] == 10833


def test_fip_usa_vl_quota_classe(monkeypatch, tmp_path):
    monkeypatch.setattr(cvm, "DIRETORIO", tmp_path)
    _escrever(
        tmp_path / "inf_quadrimestral_fip_2026.csv",
        FIP,
        [
            ["CLASSES - FIP", "35.640.741/0001-11", "BTG INFRA", "2026-04-30", "380426153.05", "", ""],
            ["CLASSES - FIP", "35.640.741/0001-11", "BTG INFRA", "2026-08-31", "392731497.87", "36.7250724", "10757566.85"],
        ],
    )

    fundo = cvm.ler_fip(2026)["35.640.741/0001-11"]

    assert fundo["vpa"] == pytest.approx(36.7250724)
    assert fundo["data_ref"] == "2026-08-31"
    assert fundo["cotas"] == pytest.approx(10757566.85)


def test_meses_recentes_acompanham_a_virada_do_mes_e_do_ano(monkeypatch):
    class DataFalsa:
        hoje = date(2026, 11, 1)

        @classmethod
        def today(cls):
            return cls.hoje

        def __call__(self, *args, **kwargs):
            return date(*args, **kwargs)

    falsa = DataFalsa()
    monkeypatch.setattr(cvm, "date", falsa)

    assert cvm._meses_recentes() == ["202611", "202610"]
    DataFalsa.hoje = date(2027, 1, 2)
    assert cvm._meses_recentes() == ["202701", "202612"]


def test_vpa_do_fii_usa_a_cvm_quando_falta_o_calculado():
    pacote = avaliar_fundamentalista({"vpa_cvm": 100.65}, classe="fii")

    assert pacote["vpa"] == 100.65


def test_vpa_da_acao_sem_cvm_continua_none():
    assert avaliar_fundamentalista({}, classe="acao_b3")["vpa"] is None


def test_config_padrao_de_liquidez_por_classe():
    assert estrategias.CONFIG_PADRAO["fiis"]["liquidez_min"] == 5_000.0
    assert estrategias.CONFIG_PADRAO["acoes"]["liquidez_min"] == 200_000.0
    assert estrategias.CONFIG_PADRAO["bdr"]["liquidez_min"] == 100_000.0
    assert estrategias.CONFIG_PADRAO["etfs"]["liquidez_min"] == 50_000.0


def test_casar_por_nome_acha_o_fundo_parecido():
    fundos = {
        "11.111.111/0001-11": {"nome": "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIARIO"},
        "22.222.222/0001-22": {"nome": "KINEA RENDIMENTOS IMOBILIARIOS FII RESPONSABILIDADE LIMITADA"},
    }

    assert (
        cvm.casar_por_nome("HEDGE Brasil Shopping Fundo de Investimento Imobiliário", fundos)
        == "11.111.111/0001-11"
    )


def test_casar_por_nome_recusa_quando_o_nome_nao_se_parece():
    fundos = {
        "11.111.111/0001-11": {"nome": "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIARIO"},
    }

    assert cvm.casar_por_nome("FUNDO TOTALMENTE DIFERENTE DE GALPOES", fundos) is None
