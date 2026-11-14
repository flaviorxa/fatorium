import base64

import pytest

USUARIO_TESTE = "teste"
SENHA_TESTE = "senha-de-teste-bem-longa-123"
CABECALHO_TESTE = {
    "Authorization": "Basic "
    + base64.b64encode(f"{USUARIO_TESTE}:{SENHA_TESTE}".encode("utf-8")).decode("ascii")
}


@pytest.fixture(autouse=True)
def _ambiente_de_autenticacao(monkeypatch):
    monkeypatch.setenv("INVEST_USUARIO", USUARIO_TESTE)
    monkeypatch.setenv("INVEST_SENHA", SENHA_TESTE)
