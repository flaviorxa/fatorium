import json

import pytest

from scripts import coletar_cnpj_clubefii as clubefii

LINHAS = [
    "| [003H11](https://www.clubefii.com.br/fiis/003H11) | "
    "[30.816.679/0001-52](https://www.clubefii.com.br/fiis/003H11) |",
    "| [AATH11](https://www.clubefii.com.br/fiis/AATH11) | "
    "[40.884.088/0001-76](https://www.clubefii.com.br/fiis/AATH11) |",
    "| [KNIP11](https://www.clubefii.com.br/fiis/KNIP11) | "
    "[24.960.430/0001-13](https://www.clubefii.com.br/fiis/KNIP11) |",
]
LIXO = [
    "# Lista de fundos",
    "| [XPTO11](https://www.clubefii.com.br/fiis/XPTO11) | sem cnpj |",
]


def test_parser_devolve_apenas_pares_validos():
    pares = clubefii.extrair_pares("\n".join(LINHAS + LIXO))

    assert pares == [
        ("003H11", "30.816.679/0001-52"),
        ("AATH11", "40.884.088/0001-76"),
        ("KNIP11", "24.960.430/0001-13"),
    ]


def test_resposta_curta_levanta_erro_e_nao_escreve(tmp_path):
    cnpj_path = tmp_path / "cnpj_clubefii.json"
    mapa_path = tmp_path / "mapa_fii_cnpj.json"

    with pytest.raises(RuntimeError):
        clubefii.processar(
            "\n".join(LINHAS + LIXO),
            cnpj_path=cnpj_path,
            mapa_path=mapa_path,
            universo={"KNIP11": "fii"},
        )

    assert not cnpj_path.exists()
    assert not mapa_path.exists()


def test_merge_preserva_nome_cvm_e_entradas_antigas(tmp_path):
    mapa_path = tmp_path / "mapa_fii_cnpj.json"
    mapa_path.write_text(
        json.dumps(
            {
                "KNIP11": {
                    "cnpj": "00.000.000/0000-00",
                    "nome_cvm": "KINEA INDICES DE PRECOS",
                    "similaridade": 1.0,
                },
                "OLD11": {"cnpj": "11.111.111/1111-11"},
            }
        ),
        encoding="utf-8",
    )

    cnpjs = {"KNIP11": "24.960.430/0001-13", "ABCD11": "22.222.222/2222-22"}
    mapa = clubefii.mesclar(
        clubefii.ler_mapa(mapa_path), cnpjs, ["KNIP11", "ABCD11"]
    )

    assert mapa["KNIP11"]["cnpj"] == "24.960.430/0001-13"
    assert mapa["KNIP11"]["nome_cvm"] == "KINEA INDICES DE PRECOS"
    assert mapa["KNIP11"]["similaridade"] == 1.0
    assert mapa["OLD11"] == {"cnpj": "11.111.111/1111-11"}
    assert mapa["ABCD11"] == {
        "cnpj": "22.222.222/2222-22",
        "fonte": "clubefii",
    }


def test_processar_e_idempotente(tmp_path):
    linhas = [
        f"| [{chr(65 + i % 26)}AB{i:02d}11](https://x) | "
        f"[{i:02d}.{i:03d}.{i:03d}/0001-{i:02d}](https://x) |"
        for i in range(100)
    ]
    cnpj_path = tmp_path / "cnpj_clubefii.json"
    mapa_path = tmp_path / "mapa_fii_cnpj.json"
    universo = {"AAB0011": "fii", "OUTRA3": "acao_b3"}

    clubefii.processar(
        "\n".join(linhas),
        cnpj_path=cnpj_path,
        mapa_path=mapa_path,
        universo=universo,
    )
    primeira = (cnpj_path.read_bytes(), mapa_path.read_bytes())

    clubefii.processar(
        "\n".join(linhas),
        cnpj_path=cnpj_path,
        mapa_path=mapa_path,
        universo=universo,
    )

    assert (cnpj_path.read_bytes(), mapa_path.read_bytes()) == primeira
    mapa = json.loads(mapa_path.read_text(encoding="utf-8"))
    assert set(mapa) == {"AAB0011"}
    assert mapa["AAB0011"]["fonte"] == "clubefii"
