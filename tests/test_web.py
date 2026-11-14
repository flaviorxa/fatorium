import json
import re
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.api.main import app
from app.motor import rankings
from tests.conftest import CABECALHO_TESTE


def _inserir_ativo(ticker: str, nome: str = "Empresa", preco_final: float = 20.0):
    db.upsert_ativo(ticker, nome=nome, classe="acao_b3", setor="Energia")
    datas = pd.bdate_range("2024-01-01", periods=300)
    fechamento = np.linspace(10.0, preco_final, len(datas))
    db.gravar_cotacoes(
        ticker,
        pd.DataFrame(
            {
                "data": datas,
                "abertura": fechamento,
                "maxima": fechamento * 1.01,
                "minima": fechamento * 0.99,
                "fechamento": fechamento,
                "volume": 1_000_000.0,
            }
        ),
    )
    db.gravar_fundos(
        ticker,
        "2026-10-28",
        {"preco": preco_final, "pl": 10.0, "pvp": 1.0, "dy": 5.0},
        "fundamentus",
    )


def _inserir_ranking(
    ticker: str = "PETR4",
    estrategia: str = "graham",
    dados_extra: dict | None = None,
) -> None:
    dados = {
        "ticker": ticker,
        "nome": "Empresa",
        "classe": "acao_b3",
        "setor": "Energia",
        "preco": 20.0,
        "variacao_pct": 1.0,
        "indicadores": {"pl": 10.0, "pvp": 1.0, "dy": 5.0},
        "posicao": 1,
        "pontuacao": 80.0,
        "aprovados": ["pl"],
        "reprovados": [],
        "indisponiveis": [],
        "dados": {},
    }
    dados.update(dados_extra or {})
    motivos = {"pl": "P/L = 10; P/L <= 15"}
    with db.conectar() as conn:
        cursor = conn.execute(
            "INSERT INTO rankings (estrategia, gerado_em, config_json, universo_n) "
            "VALUES (?, ?, ?, ?)",
            (estrategia, "2026-10-28", "{}", 2),
        )
        conn.execute(
            "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao, "
            "dados_json, motivos_json, fora_da_lista) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                int(cursor.lastrowid),
                ticker,
                1,
                80.0,
                json.dumps(dados, ensure_ascii=False),
                json.dumps(motivos, ensure_ascii=False),
            ),
        )


@pytest.fixture()
def cliente(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_ativo("PETR4", "Petrobras")
    _inserir_ativo("VALE3", "Vale")
    _inserir_ranking()
    with TestClient(app, headers=CABECALHO_TESTE) as test_client:
        yield test_client


def test_coleta_manual_refaz_os_rankings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    chamadas = {}

    monkeypatch.setattr(
        "scripts.coletar.coletar",
        lambda *a, **k: {"falhas": 0, "por_ticker": {}},
    )
    monkeypatch.setattr(
        "app.agenda._job_rankings",
        lambda: chamadas.setdefault("rankings", True),
    )
    id_coleta = db.abrir_coleta_ou_recusar("manual")

    from app.api import main as api_main

    api_main._executar_coleta(id_coleta, ["PETR4"], "5d")

    assert chamadas.get("rankings") is True


def test_coleta_manual_com_falha_nao_refaz_os_rankings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    chamadas = {}

    def explode(*a, **k):
        raise RuntimeError("sem rede")

    monkeypatch.setattr("scripts.coletar.coletar", explode)
    monkeypatch.setattr(
        "app.agenda._job_rankings",
        lambda: chamadas.setdefault("rankings", True),
    )
    id_coleta = db.abrir_coleta_ou_recusar("manual")

    from app.api import main as api_main

    api_main._executar_coleta(id_coleta, ["PETR4"], "5d")

    assert "rankings" not in chamadas


def test_dashboard_mostra_numero_de_ativos(cliente):
    resposta = cliente.get("/")
    assert resposta.status_code == 200
    assert '<strong class="cartao-numero">2</strong>' in resposta.text
    assert "Primeiras posições por ranking" in resposta.text


def test_ativo_mostra_na_com_motivo(cliente):
    resposta = cliente.get("/ativos/PETR4")
    assert resposta.status_code == 200
    assert "N/A" in resposta.text
    assert 'title="capital investido ausente"' in resposta.text
    assert "Por que está na lista" in resposta.text


def test_tecnico_semanal_rotula_semanas(cliente):
    resposta = cliente.get("/ativos/PETR4/tecnico")
    assert resposta.status_code == 200
    assert "9 semanas" in resposta.text
    assert "SMA9 diária" not in resposta.text


def test_ticker_inexistente_tem_pagina_amigavel(cliente):
    resposta = cliente.get("/ativos/NAOEXISTE")
    assert resposta.status_code == 404
    assert "<html" in resposta.text
    assert "não encontrado" in resposta.text
    assert "CVM/Anbima" in resposta.text


def test_ordem_invalida_nao_quebra(cliente):
    resposta = cliente.get("/rankings?ordem=inexistente&dir=abc")
    assert resposta.status_code == 200


def test_paginas_tem_idioma_e_aviso_cvm(cliente):
    for caminho in (
        "/",
        "/rankings",
        "/rankings/graham",
        "/screening",
        "/ativos",
        "/ativos/PETR4",
        "/comparar?tickers=PETR4,VALE3",
        "/estrategias",
        "/fontes",
    ):
        resposta = cliente.get(caminho)
        assert resposta.status_code == 200, caminho
        assert 'lang="pt-BR"' in resposta.text, caminho
        assert "CVM/Anbima" in resposta.text, caminho


def test_api_screening_devolve_as_seis_listas(cliente):
    resposta = cliente.get("/api/screening?classe=acao_b3")

    assert resposta.status_code == 200
    dados = resposta.json()
    assert set(dados) == {
        "cruzamento_alta",
        "cruzamento_baixa",
        "alinhamento_alta",
        "alinhamento_baixa",
        "pullback_alta",
        "esticado",
    }


def test_ativo_mostra_pl_com_data_e_fonte_do_campo(cliente):
    db.upsert_ativo("PRIO3", nome="PetroRio", classe="acao_b3", setor="Energia")
    db.gravar_cotacoes(
        "PRIO3",
        pd.DataFrame(
            [
                {
                    "data": "2026-10-29",
                    "abertura": 30.0,
                    "maxima": 31.0,
                    "minima": 29.0,
                    "fechamento": 30.0,
                    "volume": 1_000_000.0,
                }
            ]
        ),
        "yahoo",
    )
    db.gravar_fundos("PRIO3", "2026-10-27", {"pl": 6.5}, "fundamentus")
    db.gravar_fundos("PRIO3", "2026-10-29", {"preco": 30.0}, "yahoo")

    resposta = cliente.get("/ativos/PRIO3")

    assert resposta.status_code == 200
    trecho = resposta.text.split("P/L", 1)[1].split("ROE", 1)[0]
    assert "6,50" in trecho
    assert "fundamentus" in trecho
    assert "27/10/2026" in trecho


def test_api_ativo_usa_campo_mais_recente(cliente):
    db.gravar_fundos("PETR4", "2026-10-29", {"preco": 21.0}, "yahoo")

    resposta = cliente.get("/api/ativos/PETR4")

    assert resposta.status_code == 200
    assert resposta.json()["indicadores"]["pl"] == 10.0
    assert resposta.json()["fundamentos"]["campos"]["pl"] == 10.0


def test_dashboard_conta_pares_distintos_de_falha(cliente):
    for _ in range(3):
        db.registrar_falha("yahoo", "PETR4", "historico", "timeout")

    resposta = cliente.get("/")

    assert resposta.status_code == 200
    assert "3 registros · 1 ativos/fontes distintos" in resposta.text


def test_dashboard_exclui_mapeamento_de_fii_ja_resolvido(
    cliente, monkeypatch, tmp_path
):
    monkeypatch.setenv("INVEST_MAPA_FII", str(_mapa_fii(tmp_path, ["KNIP11"])))
    db.registrar_falha(
        "cvm", "KNIP11", "mapeamento", "ticker sem CNPJ mapeado"
    )
    db.registrar_falha(
        "cvm", "XPTO11", "mapeamento", "ticker sem CNPJ mapeado"
    )
    db.registrar_falha("yahoo", "PETR4", "historico", "timeout")

    resposta = cliente.get("/")

    assert resposta.status_code == 200
    assert "2 registros · 2 ativos/fontes distintos" in resposta.text


def test_alias_magic_formula_ev_ebit_redireciona(cliente):
    resposta = cliente.get(
        "/rankings/magic_formula_ev_ebit", follow_redirects=False
    )

    assert resposta.status_code == 302
    assert resposta.headers["location"] == "/rankings/magic_formula"


def test_rankings_mostra_lista_unica_com_colunas_da_magic_formula(cliente):
    _inserir_ranking(
        "VALE3",
        "magic_formula",
        {
            "dados": {
                "earnings_yield": 0.2,
                "return_on_capital": 0.4,
                "ev_ebit": 5.0,
                "ebit_ev_pct": 20.0,
            }
        },
    )

    resposta = cliente.get("/rankings")

    assert resposta.status_code == 200
    assert "EV/EBIT × ROIC" not in resposta.text
    assert "Magic Formula" in resposta.text
    assert "EV/EBIT" in resposta.text
    assert "EBIT/EV (%)" in resposta.text


def test_pagina_magic_formula_mostra_ev_ebit_e_ebit_ev_pct(cliente):
    _inserir_ranking(
        "VALE3",
        "magic_formula",
        {
            "dados": {
                "earnings_yield": 0.2,
                "return_on_capital": 0.4,
                "ev_ebit": 5.0,
                "ebit_ev_pct": 20.0,
            }
        },
    )

    resposta = cliente.get("/rankings/magic_formula")

    assert resposta.status_code == 200
    assert "EV/EBIT" in resposta.text
    assert "EBIT/EV (%)" in resposta.text
    assert "5,00" in resposta.text
    assert "20,00%" in resposta.text


def _inserir_consenso_petr4() -> None:
    db.gravar_fundos(
        "PETR4",
        "2026-10-14",
        {
            "consenso_total": 13,
            "consenso_compra": 12,
            "consenso_neutro": 1,
            "consenso_venda": 0,
            "alvo_medio": 60.38,
            "alvo_mediana": 62.0,
            "alvo_max": 70.0,
            "alvo_min": 47.0,
        },
        "msn",
    )


def _adicionar_item_ranking(estrategia: str, ticker: str, posicao: int) -> None:
    dados = {
        "ticker": ticker,
        "classe": "acao_b3",
        "preco": 30.0,
        "indicadores": {"pl": 8.0, "pvp": 2.0, "dy": 3.0},
        "posicao": posicao,
        "pontuacao": 70.0,
        "aprovados": [],
        "reprovados": [],
        "indisponiveis": [],
        "dados": {"ev_ebit": 6.0, "ebit_ev_pct": 16.0},
    }
    with db.conectar() as conn:
        ranking_id = conn.execute(
            "SELECT MAX(id) FROM rankings WHERE estrategia = ?", (estrategia,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ranking_itens (ranking_id, ticker, posicao, pontuacao, "
            "dados_json, motivos_json, fora_da_lista) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                ranking_id,
                ticker,
                posicao,
                70.0,
                json.dumps(dados, ensure_ascii=False),
                "{}",
            ),
        )


def test_ranking_sem_consenso_nao_mostra_colunas(cliente):
    resposta = cliente.get("/rankings/graham")

    assert resposta.status_code == 200
    assert ">Consenso<" not in resposta.text
    assert ">Alvo<" not in resposta.text


def test_ranking_misto_mostra_consenso_com_na_para_quem_nao_tem(cliente):
    _inserir_consenso_petr4()
    _inserir_ranking(
        "PETR4",
        "magic_formula",
        {
            "preco": 52.0,
            "dados": {"ev_ebit": 5.0, "ebit_ev_pct": 20.0},
        },
    )
    _adicionar_item_ranking("magic_formula", "VALE3", 2)

    resposta = cliente.get("/rankings/magic_formula")

    assert resposta.status_code == 200
    assert ">Consenso<" in resposta.text
    assert ">Alvo<" in resposta.text
    assert "12C · 1N · 0V" in resposta.text
    assert "R$ 60,38 · +16,1%" in resposta.text
    assert "N/A" in resposta.text


def test_ranking_respeita_colunas_pedidas(cliente):
    resposta = cliente.get("/rankings/graham?cols=ticker,consenso,alvo")

    assert resposta.status_code == 200
    assert "Posição" not in resposta.text
    assert "Critérios atendidos" not in resposta.text
    assert "Consenso" in resposta.text
    assert "Alvo" in resposta.text


def test_ativo_com_alvo_de_outra_classe_nao_mostra_upside(cliente):
    _inserir_ativo("TAEE4", "Taesa", preco_final=13.41)
    db.gravar_fundos(
        "TAEE4",
        "2026-10-14",
        {
            "consenso_total": 8,
            "consenso_compra": 6,
            "consenso_neutro": 2,
            "consenso_venda": 0,
            "alvo_medio": 37.37,
            "alvo_mediana": 37.0,
            "alvo_max": 40.0,
            "alvo_min": 35.0,
        },
        "msn",
    )

    resposta = cliente.get("/ativos/TAEE4")

    assert resposta.status_code == 200
    assert "+178" not in resposta.text
    assert "classe em dúvida" in resposta.text


def test_ativo_mostra_total_do_consenso_uma_vez(cliente):
    _inserir_consenso_petr4()

    resposta = cliente.get("/ativos/PETR4")

    assert resposta.status_code == 200
    assert resposta.text.count("analista(s)") == 1
    assert "13 analista(s)" in resposta.text
    assert "12 (92%)" in resposta.text
    assert "1 (8%)" in resposta.text
    assert "0 (0%)" in resposta.text
    assert "14/10/2026" in resposta.text


def test_pagina_upside_consenso_mostra_colunas_e_aviso(cliente):
    _inserir_ranking(
        "VALE3",
        "upside_consenso",
        {
            "preco": 50.0,
            "dados": {
                "preco": 50.0,
                "alvo_medio": 65.0,
                "alvo_mediana": 64.0,
                "upside_pct": 30.0,
                "consenso_total": 6,
                "consenso_compra": 4,
                "consenso_neutro": 2,
                "consenso_venda": 0,
                "data_consenso": "2026-10-28",
            },
        },
    )

    resposta = cliente.get("/rankings/upside_consenso")

    assert resposta.status_code == 200
    assert "Upside" in resposta.text
    assert "Alvo médio" in resposta.text
    assert "Analistas (C/N/V)" in resposta.text
    assert "Data do consenso" in resposta.text
    assert "4C · 2N · 0V" in resposta.text
    assert "+30,0%" in resposta.text
    assert "preço-alvo é opinião de analistas, não cálculo do sistema" in resposta.text
    assert "consenso muda sem aviso" in resposta.text
    assert "A lista tem apenas 1 ativo(s) elegível(is)" in resposta.text


def test_rankings_mostra_secao_do_maior_upside(cliente):
    _inserir_ranking(
        "VALE3",
        "upside_consenso",
        {
            "preco": 50.0,
            "dados": {
                "preco": 50.0,
                "alvo_medio": 65.0,
                "upside_pct": 30.0,
                "consenso_total": 6,
                "consenso_compra": 4,
                "consenso_neutro": 2,
                "consenso_venda": 0,
                "data_consenso": "2026-10-28",
            },
        },
    )

    resposta = cliente.get("/rankings")

    assert resposta.status_code == 200
    assert "Maior upside (consenso MSN)" in resposta.text
    assert "Alvo médio" in resposta.text
    assert "Upside" in resposta.text


def _inserir_upside_vale3() -> None:
    _inserir_ranking(
        "VALE3",
        "upside_consenso",
        {
            "preco": 50.0,
            "dados": {
                "preco": 50.0,
                "alvo_medio": 92.22,
                "alvo_mediana": 90.0,
                "upside_pct": 84.44657294207445,
                "consenso_compra": 6,
                "consenso_neutro": 0,
                "consenso_venda": 0,
                "consenso_total": 6,
                "data_consenso": "2026-10-28",
            },
        },
    )


def _recorte_dashboard(texto: str, titulo: str) -> str:
    inicio = texto.find(titulo)
    fim = texto.find("</article>", inicio)
    return texto[inicio:fim]


def test_dashboard_upside_mostra_colunas_proprias(cliente):
    _inserir_upside_vale3()

    resposta = cliente.get("/")

    assert resposta.status_code == 200
    secao = _recorte_dashboard(resposta.text, "Maior upside (consenso MSN)")
    assert "Alvo médio" in secao
    assert "Upside" in secao
    assert "Preço" in secao
    assert "Analistas" in secao
    assert "P/VP" not in secao


def test_dashboard_graham_mantem_colunas_genericas(cliente):
    resposta = cliente.get("/")

    assert resposta.status_code == 200
    secao = _recorte_dashboard(resposta.text, ">Graham<")
    assert "P/L" in secao
    assert "P/VP" in secao


def test_dashboard_upside_mostra_preco_alvo_upside_e_analistas(cliente):
    _inserir_upside_vale3()

    resposta = cliente.get("/")

    assert resposta.status_code == 200
    secao = _recorte_dashboard(resposta.text, "Maior upside (consenso MSN)")
    assert "R$ 50,00" in secao
    assert "R$ 92,22" in secao
    assert "+84,4%" in secao
    assert "6C/0N/0V · 6" in secao
    assert "28/10/2026" in secao
    assert "preço-alvo é opinião de analistas (MSN), não cálculo do sistema" in secao


def test_dashboard_upside_sem_dados_de_alvo_mostra_na(cliente):
    _inserir_upside_vale3()
    _adicionar_item_ranking("upside_consenso", "PETR4", 2)

    resposta = cliente.get("/")

    assert resposta.status_code == 200
    secao = _recorte_dashboard(resposta.text, "Maior upside (consenso MSN)")
    assert "N/A" in secao


def test_post_atualizar_cria_coleta(cliente, monkeypatch):
    chamadas = []

    def coletar_falso(tickers, periodo="5d", **_):
        chamadas.append((list(tickers), periodo))
        return {"total": len(tickers), "ok": len(tickers), "falhas": 0}

    monkeypatch.setattr("scripts.coletar.coletar", coletar_falso)
    antes = db.consultar("SELECT COUNT(*) AS total FROM coletas")[0]["total"]

    resposta = cliente.post(
        "/atualizar",
        data={"tickers": "PETR4", "periodo": "5d"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    linhas = db.consultar("SELECT id, tipo, status FROM coletas ORDER BY id")
    assert len(linhas) == antes + 1
    assert linhas[-1]["status"] in {"em_andamento", "ok", "parcial"}
    assert chamadas == [(["PETR4"], "5d")]


def _lista_fii(tmp_path, tickers):
    arquivo = tmp_path / "lista-de-ativos.md"
    arquivo.write_text(
        "## Fundos Imobiliários e Fi-Infras "
        f"({len(tickers)})\n{', '.join(tickers)}\n",
        encoding="utf-8",
    )
    return arquivo


def _mapa_fii(tmp_path, tickers):
    arquivo = tmp_path / "mapa_fii_cnpj.json"
    arquivo.write_text(
        json.dumps({ticker: {"cnpj": "00.000.000/0001-00"} for ticker in tickers}),
        encoding="utf-8",
    )
    return arquivo


def test_api_fontes_nao_deixa_historico_de_mapeamento_vazar(
    cliente, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        config, "LISTA_ATIVOS", _lista_fii(tmp_path, ["KNIP11", "HGLG11"])
    )
    monkeypatch.setenv(
        "INVEST_MAPA_FII", str(_mapa_fii(tmp_path, ["KNIP11", "HGLG11"]))
    )
    for ticker in ("KNIP11", "HGLG11"):
        db.registrar_falha(
            "cvm", ticker, "mapeamento", "ticker sem CNPJ mapeado"
        )

    resposta = cliente.get("/api/fontes")

    assert resposta.status_code == 200
    mapeamento = resposta.json()["mapeamento_fii"]
    assert mapeamento["tickers_pendentes"] == []
    assert mapeamento["pendentes"] == 0
    assert mapeamento["texto"] == "todos os FIIs do universo têm CNPJ mapeado"


def test_api_fontes_lista_so_o_fii_do_universo_fora_do_mapa(
    cliente, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        config, "LISTA_ATIVOS", _lista_fii(tmp_path, ["KNIP11", "HGLG11"])
    )
    monkeypatch.setenv("INVEST_MAPA_FII", str(_mapa_fii(tmp_path, ["KNIP11"])))
    db.registrar_falha(
        "cvm", "KNIP11", "mapeamento", "ticker sem CNPJ mapeado"
    )

    resposta = cliente.get("/api/fontes")

    mapeamento = resposta.json()["mapeamento_fii"]
    assert mapeamento["tickers_pendentes"] == ["HGLG11"]
    assert mapeamento["pendentes"] == 1
    assert (
        mapeamento["texto"]
        == "1 FII(s) sem CNPJ: use scripts/coletar_cnpj_clubefii.py"
    )


def test_pagina_fontes_mostra_mapeamento_de_fii(cliente, monkeypatch, tmp_path):
    monkeypatch.setattr(
        config, "LISTA_ATIVOS", _lista_fii(tmp_path, ["KNIP11", "HGLG11"])
    )
    monkeypatch.setenv(
        "INVEST_MAPA_FII", str(_mapa_fii(tmp_path, ["KNIP11", "HGLG11"]))
    )

    resposta = cliente.get("/fontes")

    assert resposta.status_code == 200
    assert "Mapeamento de FIIs" in resposta.text
    assert "todos os FIIs do universo têm CNPJ mapeado" in resposta.text


def test_fontes_recarrega_enquanto_coleta_esta_em_andamento(cliente):
    assert db.abrir_coleta_ou_recusar("manual") is not None

    resposta = cliente.get("/fontes")

    assert resposta.status_code == 200
    assert 'http-equiv="refresh"' in resposta.text
    assert "a página se atualiza" in resposta.text


def test_fontes_mostra_resultado_e_nao_recarrega_apos_concluir(cliente):
    id_coleta = db.abrir_coleta_ou_recusar("manual")
    assert id_coleta is not None
    db.finalizar_coleta(
        id_coleta, "parcial", {"ok": 113, "falhas": 11, "total": 124}
    )

    resposta = cliente.get("/fontes")

    assert resposta.status_code == 200
    assert 'http-equiv="refresh"' not in resposta.text
    assert "113 ok · 11 falhas de 124" in resposta.text


def test_fontes_sem_coleta_nao_recarrega_nem_mostra_numeros(cliente):
    resposta = cliente.get("/fontes")

    assert resposta.status_code == 200
    assert 'http-equiv="refresh"' not in resposta.text
    assert "113 ok" not in resposta.text
    assert "Nenhuma coleta registrada." in resposta.text


def test_salvar_agendamento_grava_e_aplica(cliente):
    resposta = cliente.post(
        "/fontes/agenda",
        data={"frequencia": "semanal", "horario": "07:30"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    assert db.ler_configuracao("agenda_frequencia") == "semanal"
    assert db.ler_configuracao("agenda_horario") == "07:30"


def test_salvar_agendamento_invalido_nao_grava(cliente):
    cliente.post(
        "/fontes/agenda",
        data={"frequencia": "dias_uteis", "horario": "07:30"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    resposta = cliente.post(
        "/fontes/agenda",
        data={"frequencia": "dias_uteis", "horario": "22:50"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    assert db.ler_configuracao("agenda_horario") == "07:30"


def test_pagina_fontes_mostra_coleta_automatica(cliente):
    resposta = cliente.get("/fontes")

    assert resposta.status_code == 200
    assert "Coleta automática" in resposta.text
    assert "Salvar agendamento" in resposta.text
    assert "Próxima coleta automática:" in resposta.text


def _inserir_fii(ticker: str, campos: dict) -> None:
    db.upsert_ativo(ticker, nome=ticker, classe="fii")
    datas = pd.bdate_range("2025-01-01", periods=60)
    fechamento = np.linspace(10.0, 10.5, len(datas))
    db.gravar_cotacoes(
        ticker,
        pd.DataFrame(
            {
                "data": datas,
                "abertura": fechamento,
                "maxima": fechamento * 1.01,
                "minima": fechamento * 0.99,
                "fechamento": fechamento,
                "volume": 1_000_000.0,
            }
        ),
    )
    db.gravar_fundos(ticker, "2026-10-30", campos, "fundsexplorer")


def test_pagina_de_fii_mostra_vacancia_e_imoveis(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_fii(
        "HGBS11",
        {
            "dy": 10.89,
            "vacancia": 4.4,
            "vacancia_data_ref": "2026-08-01",
            "imoveis": 15,
        },
    )
    _inserir_fii("KNCR11", {"dy": 13.33})

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        com_vacancia = cliente_teste.get("/ativos/HGBS11").text
        sem_vacancia = cliente_teste.get("/ativos/KNCR11").text

    assert "Vacância física (01/08/2026)" in com_vacancia
    assert ">15<" in re.sub(r"\s+", "", com_vacancia)
    assert "Vacância física" not in sem_vacancia


def _inserir_etf(ticker: str, campos: dict) -> None:
    db.upsert_ativo(ticker, nome=ticker, classe="etf")
    datas = pd.bdate_range("2025-01-01", periods=60)
    fechamento = np.linspace(20.0, 21.0, len(datas))
    db.gravar_cotacoes(
        ticker,
        pd.DataFrame(
            {
                "data": datas,
                "abertura": fechamento,
                "maxima": fechamento * 1.01,
                "minima": fechamento * 0.99,
                "fechamento": fechamento,
                "volume": 1_000_000.0,
            }
        ),
    )
    db.gravar_fundos(ticker, "2026-10-30", campos, "statusinvest")


def test_pagina_de_etf_mostra_patrimonio_taxa_e_cotistas(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_etf(
        "ACWI11",
        {
            "patrimonio_liquido": 242018551.0,
            "taxa_administracao_etf": 0.3,
            "cotistas": 15069.0,
        },
    )

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        pagina = cliente_teste.get("/ativos/ACWI11").text

    assert "Taxa de administração" in pagina
    assert "0,30%" in pagina
    assert "Cotistas" in pagina
    assert "Patrimônio líquido" in pagina


def test_tecnico_traz_bandas_de_bollinger_na_periodicidade_do_grafico(cliente):
    resposta = cliente.get("/ativos/PETR4/tecnico")
    assert resposta.status_code == 200
    assert "bandas de Bollinger" in resposta.text
    assert '"bandas"' in resposta.text
    assert '"superior"' in resposta.text
    assert '"inferior"' in resposta.text


def test_ativo_mostra_estimativas_e_links_externos(cliente):
    from datetime import date, timedelta

    _inserir_ativo("CMIG4", "Cemig", preco_final=20.0)
    db.gravar_fundos(
        "CMIG4",
        "2026-10-30",
        {"lpa": 2.0, "vpa_calculado": 10.0},
        "fundamentus",
    )
    db.gravar_fundos(
        "CMIG4",
        "2026-10-31",
        {
            "genial_alvo": 25.0,
            "genial_preco": 20.0,
            "genial_preco_data": "2026-10-30",
            "genial_recomendacao": "Compra",
        },
        "genial",
    )
    db.gravar_fundos(
        "CMIG4",
        "2026-10-31",
        {
            "btg_alvo": 28.0,
            "btg_data": "2026-10-30",
            "btg_recomendacao": "Compra",
        },
        "btg",
    )
    db.gravar_proventos(
        "CMIG4",
        pd.DataFrame(
            {
                "data_ex": [(date.today() - timedelta(days=20)).isoformat()],
                "valor": [1.2],
                "tipo": ["dividendo"],
            }
        ),
        "yahoo",
    )

    resposta = cliente.get("/ativos/CMIG4")

    assert resposta.status_code == 200
    assert "Estimativas de valor" in resposta.text
    assert "Bazin (DY 6%)" in resposta.text
    assert "R$ 20,00" in resposta.text  # 1,20 / 6%
    assert "Graham" in resposta.text
    assert "R$ 21,21" in resposta.text  # raiz(22,5 x 2,00 x 10,00)
    assert "Genial (alvo 12m)" in resposta.text
    assert "R$ 25,00" in resposta.text
    assert "recomendação Compra" in resposta.text
    assert "BTG (alvo 12m)" in resposta.text
    assert "R$ 28,00" in resposta.text
    assert "https://investidor10.com.br/acoes/CMIG4/" in resposta.text
    assert "https://statusinvest.com.br/acoes/cmig4" in resposta.text
    assert "https://analisa.genialinvestimentos.com.br/acoes/CMIG4/" in resposta.text


def _inserir_fii_13_meses(ticker: str = "HGLG11") -> None:
    hoje = date.today()
    dias = [400 - indice * 30 for indice in range(14)] + [1]
    fechamento = np.concatenate(
        ([100.0, 100.0], np.linspace(100.0, 110.0, len(dias) - 2))
    )
    db.upsert_ativo(ticker, nome=ticker, classe="fii")
    db.gravar_cotacoes(
        ticker,
        pd.DataFrame(
            {
                "data": [hoje - timedelta(days=dia) for dia in dias],
                "abertura": fechamento,
                "maxima": fechamento,
                "minima": fechamento,
                "fechamento": fechamento,
                "volume": 1_000_000.0,
            }
        ),
    )
    db.gravar_fundos(
        ticker,
        hoje.isoformat(),
        {
            "dy": 6.0,
            "pvp": 0.9,
            "liquidez_diaria_brl": 1_000_000.0,
            "meses_com_provento_24m": 24,
        },
        "fundsexplorer",
    )
    db.gravar_proventos(
        ticker,
        pd.DataFrame(
            {
                "data_ex": [(hoje - timedelta(days=30)).isoformat()],
                "valor": [6.0],
                "tipo": ["rendimento"],
            }
        ),
        "yahoo",
    )


def test_fii_mostra_retorno_total_12m_na_pagina(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_fii_13_meses("HGLG11")

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        pagina = cliente_teste.get("/ativos/HGLG11").text

    assert "Retorno total nos últimos 12 meses" in pagina
    assert "Retorno total 12m" in pagina
    assert "Valorização da cota 12m" in pagina
    assert "Proventos 12m" in pagina
    assert "+16,00%" in pagina
    assert "+10,00%" in pagina
    assert "+6,00%" in pagina


def test_dashboard_mostra_secao_fiis_por_retorno_total(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_fii_13_meses("HGLG11")
    rankings.gerar_rankings()

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        pagina = cliente_teste.get("/").text

    assert "FIIs por retorno total (12m)" in pagina
    secao = _recorte_dashboard(pagina, "FIIs por retorno total (12m)")
    assert "Retorno 12m" in secao
    assert "Cota 12m" in secao
    assert "Proventos 12m" in secao
    assert "+16,00%" in secao


def test_ranking_fiis_ltm_usa_as_colunas_do_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_fii_13_meses("HGLG11")
    rankings.gerar_rankings()

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        completo = cliente_teste.get("/rankings/fiis_ltm").text
        lista = cliente_teste.get("/rankings").text

    secao = _recorte_dashboard(lista, "FIIs por retorno total (12m)")
    for cabecalho in ("Retorno 12m", "Cota 12m", "Proventos 12m"):
        assert cabecalho in secao
        assert cabecalho in completo

    tabela = _recorte_dashboard(completo, "Tabela completa")
    assert "P/L" not in tabela
    assert "P/VP" not in tabela
    assert "+16,00%" in tabela


def test_envia_planilha_btg_pela_tela_de_fontes(cliente):
    import io

    from openpyxl import Workbook

    livro = Workbook()
    aba = livro.worksheets[0]
    aba.append(["Ticker", "Empresa", "Preço-alvo (R$)", "Recomendação"])
    aba.append(["PETR4", "Petrobras", 30.0, "COMPRA"])
    buffer = io.BytesIO()
    livro.save(buffer)

    pagina = cliente.get("/fontes")
    assert "Enviar planilha" in pagina.text
    assert 'enctype="multipart/form-data"' in pagina.text

    resposta = cliente.post(
        "/fontes/planilha-btg",
        files={
            "arquivo": (
                "guia.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
        headers={"Origin": "http://testserver"},
    )

    assert resposta.status_code == 303
    valores = db.consultar(
        "SELECT valor FROM fundamentos WHERE ticker = 'PETR4' "
        "AND campo = 'btg_alvo' AND fonte = 'btg_planilha'"
    )
    assert [linha["valor"] for linha in valores] == [30.0]


def test_dashboard_e_ranking_mostram_regularidade_e_meses_de_provento(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")
    _inserir_ativo("AAAA3")

    hoje = date.today()
    indice = hoje.year * 12 + hoje.month - 1 - 23
    ano, mes = divmod(indice, 12)
    datas = []
    for passo in range(24):
        a, m = divmod(ano * 12 + mes + passo, 12)
        if m + 1 == 2:
            datas.append(date(a, 2, 1).isoformat())
    db.gravar_proventos(
        "AAAA3",
        pd.DataFrame([{"data_ex": data, "valor": 1.0} for data in datas]),
    )
    rankings.gerar_rankings()

    with TestClient(app, headers=CABECALHO_TESTE) as cliente_teste:
        pagina = cliente_teste.get("/").text
        completo = cliente_teste.get("/rankings/regularidade_proventos").text
        lista = cliente_teste.get("/rankings").text
        ativo = cliente_teste.get("/ativos/AAAA3?aba=lista").text

    secao = _recorte_dashboard(pagina, "Regularidade de proventos (24m)")
    for cabecalho in (
        "Regularidade (24m)",
        "Meses com provento (de 24)",
        "Pagamentos em 24m",
        "Observação",
    ):
        assert cabecalho in secao
        assert cabecalho in completo
    assert "paga geralmente em fev" in secao
    assert "paga geralmente em fev" in completo
    assert "Regularidade de proventos (24m)" in lista
    assert "regularidade 8,3%" in ativo
    assert "2 de 24 meses" in ativo
    inicio_meses = ativo.find("Meses mais recorrentes")
    assert inicio_meses != -1
    assert "fev" in ativo[inicio_meses : inicio_meses + 200]


def test_site_tem_seletor_de_tema_claro_escuro(cliente):
    pagina = cliente.get("/").text

    assert 'id="tema"' in pagina
    assert '<option value="claro">Claro</option>' in pagina
    assert '<option value="escuro">Escuro</option>' in pagina
    # O tema vive em /static/tema.js: o CSP é script-src 'self' e bloqueia inline.
    assert "/static/tema.js?v=" in pagina
    assert "localStorage.getItem" not in pagina
    executaveis = re.findall(
        r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", pagina
    )
    assert executaveis == []
    # Assets versionados por mtime: o navegador não fica preso na folha antiga.
    assert "estilo.css?v=" in pagina

    script = cliente.get("/static/tema.js").text
    # Sem escolha salva o tema inicial vem do sistema.
    assert "prefers-color-scheme: dark" in script
    assert "dataset.tema" in script
    assert 'localStorage.setItem("tema"' in script

    css = cliente.get("/static/estilo.css").text
    # Variáveis explícitas por tema: light-dark() não resolveu no Firefox.
    assert ':root[data-tema="escuro"]' in css
    assert "light-dark(" not in css


def test_cabecalho_tem_botao_de_menu_para_telas_pequenas(cliente):
    pagina = cliente.get("/").text

    assert 'class="botao-menu"' in pagina
    assert 'aria-controls="menu-principal"' in pagina
    assert 'aria-expanded="false"' in pagina
    assert 'id="menu-principal"' in pagina


def test_pagina_inicial_carrega_o_script_do_menu(cliente):
    pagina = cliente.get("/").text

    # O CSP é script-src 'self': o comportamento vive em arquivo externo versionado.
    assert "/static/menu.js?v=" in pagina


def test_script_do_menu_existe_e_controla_aria_expanded():
    script = (config.RAIZ / "app" / "static" / "menu.js").read_text(
        encoding="utf-8"
    )

    assert "aria-expanded" in script


def test_confirmacao_de_exclusao_nao_usa_handler_inline():
    """CSP script-src 'self' bloqueia onsubmit inline — o confirm viraria no-op."""

    from app import config

    template = (config.RAIZ / "app" / "templates" / "universo.html").read_text(
        encoding="utf-8"
    )
    assert "onsubmit=" not in template
    assert "data-confirmar=" in template

    script = (config.RAIZ / "app" / "static" / "confirmar.js").read_text(
        encoding="utf-8"
    )
    assert "data-confirmar" in script
    assert "preventDefault" in script


def test_ativo_destaca_dy_entre_os_cartoes_iniciais(cliente):
    pagina = cliente.get("/ativos/PETR4").text

    assert "cartao-destaque" in pagina
    assert "Dividend yield" in pagina
    assert '<strong class="cartao-numero dy">5,00%</strong>' in pagina


def test_ativo_sem_dy_informado_usa_o_calculado_no_cartao(cliente):
    with db.conectar() as conn:
        conn.execute("DELETE FROM fundamentos WHERE ticker = 'VALE3' AND campo = 'dy'")
    db.gravar_proventos(
        "VALE3",
        pd.DataFrame(
            {
                "data_ex": [(date.today() - timedelta(days=20)).isoformat()],
                "valor": [2.0],
                "tipo": ["dividendo"],
            }
        ),
        "yahoo",
    )

    pagina = cliente.get("/ativos/VALE3").text

    assert "proventos de 12 meses / fechamento" in pagina
    assert '<strong class="cartao-numero dy">10,00%</strong>' in pagina


def test_tabela_completa_de_ranking_tem_coluna_dy_destacada(cliente):
    pagina = cliente.get("/rankings/graham").text

    assert ">DY<" in pagina
    assert '<strong class="dy">5,00%</strong>' in pagina


def test_lista_de_ativos_tem_coluna_dy_destacada(cliente):
    pagina = cliente.get("/ativos").text

    assert ">DY<" in pagina
    assert '<strong class="dy">5,00%</strong>' in pagina
