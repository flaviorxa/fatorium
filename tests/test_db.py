import pandas as pd

from app import config, db


def _usar_banco_temporario(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def test_schema_idempotente(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.aplicar_schema()
    db.aplicar_schema()
    with db.conectar() as conn:
        tabelas = {
            linha[0]
            for linha in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"ativos", "cotacoes", "fundamentos", "proventos", "coletas", "falhas", "configuracoes"} <= tabelas


def test_upsert_cotacao_nao_duplica(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    df = pd.DataFrame(
        [
            {
                "data": "2026-10-28",
                "abertura": 10.0,
                "maxima": 11.0,
                "minima": 9.5,
                "fechamento": 10.5,
                "fechamento_ajustado": 10.4,
                "volume": 1000.0,
            }
        ]
    )
    assert db.gravar_cotacoes("PETR4", df, "yahoo") == 1
    assert db.gravar_cotacoes("PETR4", df, "yahoo") == 1

    with db.conectar() as conn:
        total = conn.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0]
        linha = conn.execute("SELECT data, fechamento FROM cotacoes").fetchone()
    assert total == 1
    assert linha["data"] == "2026-10-28"
    assert linha["fechamento"] == 10.5
    assert db.tem_dados("PETR4") is True
    assert db.tem_dados("VALE3") is False


def test_gravar_fundos_formato_longo_e_idempotente(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    campos = {"pl": 5.07, "roe": 27.7, "vazio": None}
    assert db.gravar_fundos("PETR4", "2026-10-28", campos, "fundamentus") == 2
    assert db.gravar_fundos("PETR4", "2026-10-28", campos, "fundamentus") == 2

    with db.conectar() as conn:
        total = conn.execute("SELECT COUNT(*) FROM fundamentos").fetchone()[0]
        valor = conn.execute(
            "SELECT valor FROM fundamentos WHERE ticker = ? AND campo = ?",
            ("PETR4", "pl"),
        ).fetchone()[0]
    assert total == 2
    assert valor == 5.07


def test_coleta_e_falha(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    id_coleta = db.iniciar_coleta("teste")
    db.finalizar_coleta(id_coleta, "ok", {"total": 1})

    with db.conectar() as conn:
        linha = conn.execute("SELECT * FROM coletas WHERE id = ?", (id_coleta,)).fetchone()
    assert linha["status"] == "ok"
    assert linha["terminado_em"] is not None

    db.registrar_falha("yahoo", "PETR4", "historico", "timeout")
    with db.conectar() as conn:
        total = conn.execute("SELECT COUNT(*) FROM falhas").fetchone()[0]
    assert total == 1


def test_fundamentos_mais_recentes_usa_janela_por_campo(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.gravar_fundos("PETR4", "2026-10-27", {"pl": 5.07}, "fundamentus")
    db.gravar_fundos("PETR4", "2026-10-29", {"preco": 20.0}, "yahoo")

    recentes = db.fundamentos_mais_recentes("PETR4")

    assert recentes["pl"] == {
        "valor": 5.07,
        "fonte": "fundamentus",
        "data_ref": "2026-10-27",
    }
    assert recentes["preco"] == {
        "valor": 20.0,
        "fonte": "yahoo",
        "data_ref": "2026-10-29",
    }


def test_fundamentos_mais_recentes_prioriza_fonte_na_mesma_data(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.aplicar_schema()
    with db.conectar() as conn:
        conn.execute("DROP TABLE fundamentos")
        conn.execute(
            "CREATE TABLE fundamentos (ticker TEXT, data_ref TEXT, campo TEXT, "
            "valor REAL, fonte TEXT, coletado_em TEXT)"
        )
        conn.executemany(
            "INSERT INTO fundamentos (ticker, data_ref, campo, valor, fonte) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("PETR4", "2026-10-27", "pl", 1.0, "yahoo"),
                ("PETR4", "2026-10-27", "pl", 2.0, "cvm"),
                ("PETR4", "2026-10-27", "pl", 3.0, "fundamentus"),
            ],
        )

    recentes = db.fundamentos_mais_recentes("PETR4")

    assert recentes["pl"]["valor"] == 3.0
    assert recentes["pl"]["fonte"] == "fundamentus"


def test_limpar_falhas_mapeamento_resolvidas_remove_so_os_mapeados(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    mapa = tmp_path / "mapa_fii_cnpj.json"
    mapa.write_text(
        '{"KNIP11": {"cnpj": "00.000.000/0001-00"}, '
        '"HGLG11": {"cnpj": "00.000.000/0001-01"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("INVEST_MAPA_FII", str(mapa))
    for ticker in ("KNIP11", "HGLG11", "XPTO11"):
        db.registrar_falha(
            "cvm", ticker, "mapeamento", "ticker sem CNPJ mapeado"
        )
    db.registrar_falha("yahoo", "KNIP11", "historico", "timeout")

    removidas = db.limpar_falhas_mapeamento_resolvidas()

    assert removidas == 2
    restantes = db.consultar(
        "SELECT fonte, ticker, etapa FROM falhas ORDER BY fonte, ticker"
    )
    assert [dict(linha) for linha in restantes] == [
        {"fonte": "cvm", "ticker": "XPTO11", "etapa": "mapeamento"},
        {"fonte": "yahoo", "ticker": "KNIP11", "etapa": "historico"},
    ]
    assert db.limpar_falhas_mapeamento_resolvidas() == 0


def test_podar_rankings_mantem_a_ultima_de_cada_estrategia(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)

    def inserir(estrategia, gerado_em):
        with db.conectar() as conn:
            cursor = conn.execute(
                "INSERT INTO rankings (estrategia, gerado_em, config_json, universo_n)"
                " VALUES (?, ?, '{}', 1)",
                (estrategia, gerado_em),
            )
            conn.execute(
                "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao,"
                " dados_json, motivos_json, fora_da_lista)"
                " VALUES (?, 'PETR4', 1, 80, '{}', '{}', 0)",
                (int(cursor.lastrowid),),
            )

    inserir("graham", "2026-06-01T12:00:00+00:00")
    inserir("graham", "2026-07-01T12:00:00+00:00")
    inserir("graham", "2026-10-30T12:00:00+00:00")
    inserir("fiis", "2026-05-01T12:00:00+00:00")
    inserir("fiis", "2026-06-01T12:00:00+00:00")

    apagadas = db.podar_rankings(dias=30)

    restantes = db.consultar(
        "SELECT estrategia, gerado_em FROM rankings ORDER BY estrategia, gerado_em"
    )
    assert apagadas == 3
    assert [dict(linha)["gerado_em"] for linha in restantes] == [
        "2026-06-01T12:00:00+00:00",  # fiis: a mais nova dessa estratégia
        "2026-10-30T12:00:00+00:00",  # graham: a mais nova dessa estratégia
    ]
    assert db.consultar("SELECT COUNT(*) AS n FROM ranking_itens")[0]["n"] == 2
