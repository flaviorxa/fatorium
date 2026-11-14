import base64

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.web.auth import validar_ambiente
from tests.conftest import SENHA_TESTE, USUARIO_TESTE


def _cabecalho(usuario: str = USUARIO_TESTE, senha: str = SENHA_TESTE) -> dict[str, str]:
    bruto = f"{usuario}:{senha}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(bruto).decode("ascii")}


@pytest.fixture()
def cliente():
    with TestClient(app) as test_client:
        yield test_client


def test_sem_senha_recusa_subir(monkeypatch):
    monkeypatch.delenv("INVEST_SENHA", raising=False)
    with pytest.raises(RuntimeError, match="INVEST_SENHA"):
        validar_ambiente()


def test_senha_curta_recusa_subir(monkeypatch):
    monkeypatch.setenv("INVEST_SENHA", "curta-123456789")
    with pytest.raises(RuntimeError, match="INVEST_SENHA"):
        validar_ambiente()


def test_sem_authorization_devolve_401(cliente):
    resposta = cliente.get("/")
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == (
        'Basic realm="InvestIQ", charset="UTF-8"'
    )
    assert "Autenticação necessária" in resposta.text


def test_credencial_correta_abre_html_e_api(cliente):
    for caminho in ("/", "/api/ativos"):
        resposta = cliente.get(caminho, headers=_cabecalho())
        assert resposta.status_code == 200, caminho


def test_credencial_errada_devolve_401(cliente):
    resposta = cliente.get("/api/ativos", headers=_cabecalho(senha="errada-bem-longa-123"))
    assert resposta.status_code == 401
    assert "WWW-Authenticate" in resposta.headers


def test_robots_txt_autenticado_e_fechado(cliente):
    sem_senha = cliente.get("/robots.txt")
    assert sem_senha.status_code == 401

    resposta = cliente.get("/robots.txt", headers=_cabecalho())
    assert resposta.status_code == 200
    assert resposta.text == "User-agent: *\nDisallow: /\n"
    assert resposta.headers["content-type"].startswith("text/plain")


def test_x_robots_em_html_api_e_robots(cliente):
    for caminho in ("/", "/api/ativos", "/robots.txt"):
        resposta = cliente.get(caminho, headers=_cabecalho())
        assert resposta.status_code == 200, caminho
        assert resposta.headers["X-Robots-Tag"] == (
            "noindex, nofollow, noarchive, nosnippet"
        ), caminho


def test_meta_robots_no_html(cliente):
    resposta = cliente.get("/", headers=_cabecalho())
    assert '<meta name="robots" content="noindex,nofollow">' in resposta.text


def test_nenhuma_resposta_contem_a_senha(cliente):
    for caminho in ("/", "/api/ativos", "/robots.txt"):
        resposta = cliente.get(caminho, headers=_cabecalho())
        assert SENHA_TESTE not in resposta.text, caminho

    recusada = cliente.get("/", headers=_cabecalho(senha="errada-bem-longa-123"))
    assert SENHA_TESTE not in recusada.text


def test_post_com_origin_externo_devolve_403(cliente):
    resposta = cliente.post(
        "/backtests",
        headers={**_cabecalho(), "Origin": "https://evil.example"},
        data={"inicio": "invalida"},
    )
    assert resposta.status_code == 403
    assert "Origem" in resposta.text


def test_post_com_origin_do_mesmo_host_passa(cliente):
    resposta = cliente.post(
        "/backtests",
        headers={**_cabecalho(), "Origin": "http://testserver"},
        data={"inicio": "invalida"},
    )
    assert resposta.status_code != 403


def test_post_sem_origin_e_sem_referer_devolve_403(cliente):
    resposta = cliente.post(
        "/backtests",
        headers=_cabecalho(),
        data={"inicio": "invalida"},
    )
    assert resposta.status_code == 403


def test_post_com_referer_do_mesmo_host_passa(cliente):
    resposta = cliente.post(
        "/backtests",
        headers={**_cabecalho(), "Referer": "http://testserver/backtests"},
        data={"inicio": "invalida"},
    )
    assert resposta.status_code != 403


def test_get_continua_livre_sem_origin(cliente):
    resposta = cliente.get("/", headers=_cabecalho())
    assert resposta.status_code == 200

