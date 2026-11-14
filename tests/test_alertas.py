import pandas as pd
from fastapi.testclient import TestClient

from app import alertas, config, db
from app.api.main import app
from tests.conftest import CABECALHO_TESTE


def _usar_banco_temporario(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _inserir_ranking(estrategia: str, tickers: list[str], gerado_em: str) -> int:
    with db.conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO rankings (estrategia, gerado_em, config_json, universo_n) "
            "VALUES (?, ?, '{}', ?)",
            (estrategia, gerado_em, len(tickers)),
        )
        ranking_id = int(cursor.lastrowid)
        for posicao, ticker in enumerate(tickers, start=1):
            conn.execute(
                "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao, "
                "dados_json, motivos_json, fora_da_lista) "
                "VALUES (?, ?, ?, 1.0, '{}', '{}', 0)",
                (ranking_id, ticker, posicao),
            )
    return ranking_id


def test_entrar_e_sair_gera_um_alerta_por_evento(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    _inserir_ranking("graham", ["PETR4", "VALE3"], "2026-10-27")
    _inserir_ranking("graham", ["PETR4", "ITUB4"], "2026-10-28")

    alertas.gerar_alertas_rankings()

    linhas = db.consultar(
        "SELECT tipo, ticker, estrategia, data_ref FROM alertas ORDER BY tipo"
    )
    assert [dict(linha) for linha in linhas] == [
        {
            "tipo": "entrou",
            "ticker": "ITUB4",
            "estrategia": "graham",
            "data_ref": "2026-10-28",
        },
        {
            "tipo": "saiu",
            "ticker": "VALE3",
            "estrategia": "graham",
            "data_ref": "2026-10-28",
        },
    ]


def test_duplicata_e_ignorada(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)

    primeiro = alertas.criar(
        "entrou",
        ticker="PETR4",
        estrategia="graham",
        titulo="Entrou em Graham",
        motivo="critério; referência: 28/10/2026",
        data_ref="2026-10-28",
    )
    segundo = alertas.criar(
        "entrou",
        ticker="PETR4",
        estrategia="graham",
        titulo="Entrou em Graham",
        motivo="critério; referência: 28/10/2026",
        data_ref="2026-10-28",
    )

    assert primeiro is True
    assert segundo is False
    assert len(alertas.listar()) == 1


def test_desatualizado_usa_criterio_de_cinco_dias_uteis(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.upsert_ativo("PETR4", nome="Petrobras", classe="acao_b3")
    db.gravar_cotacoes(
        "PETR4",
        pd.DataFrame(
            [
                {
                    "data": "2026-10-20",
                    "abertura": 10.0,
                    "maxima": 11.0,
                    "minima": 9.0,
                    "fechamento": 10.0,
                    "volume": 1000.0,
                }
            ]
        ),
    )

    gerados = alertas.gerar_alertas_desatualizados(hoje=pd.Timestamp("2026-10-29").date())

    assert gerados == 1
    linha = alertas.listar()[0]
    assert linha["tipo"] == "desatualizado"
    assert linha["ticker"] == "PETR4"
    assert linha["data_ref"] == "2026-10-20"
    assert "mais de 5 dias úteis" in linha["motivo"]


def test_api_pagina_e_marcacao_de_lido(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    alertas.criar(
        "saiu",
        ticker="VALE3",
        estrategia="graham",
        titulo="Saiu de Graham",
        motivo="critério; referência: 28/10/2026",
        data_ref="2026-10-28",
    )

    with TestClient(app, headers=CABECALHO_TESTE) as cliente:
        nao_lidos = cliente.get("/api/alertas?lidos=0").json()
        assert len(nao_lidos) == 1
        assert nao_lidos[0]["ticker"] == "VALE3"

        agenda = cliente.get("/api/agenda").json()
        assert {item["job"] for item in agenda} == {
            "cotacoes",
            "fundamentos",
            "consenso",
            "estimativas",
            "rankings",
            "cvm_mensal",
            "frescor",
            "backup",
        }

        pagina = cliente.get("/alertas")
        assert pagina.status_code == 200
        assert "Saiu de Graham" in pagina.text

        resposta = cliente.post(
            f"/alertas/{nao_lidos[0]['id']}/lido",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert resposta.status_code == 303

    assert alertas.listar(lidos=0) == []
