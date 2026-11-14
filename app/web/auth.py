"""Autenticação HTTP Basic obrigatória e não indexação dos buscadores."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

USUARIO_PADRAO = "invest"
TAMANHO_MINIMO_SENHA = 16
DESAFIO_BASIC = 'Basic realm="FarolB3", charset="UTF-8"'
CABECALHO_ROBOTS = "noindex, nofollow, noarchive, nosnippet"
METODOS_QUE_ALTERAM = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _senha() -> str | None:
    return os.getenv("INVEST_SENHA")


def usuario() -> str:
    return os.getenv("INVEST_USUARIO", USUARIO_PADRAO)


def validar_ambiente() -> str:
    """Garante que a senha existe e é longa o bastante; devolve o usuário.

    Falha fechado: sem ``INVEST_SENHA`` válida a aplicação não deve subir.
    """

    senha = _senha()
    if senha is None or len(senha) < TAMANHO_MINIMO_SENHA:
        raise RuntimeError(
            "INVEST_SENHA ausente ou com menos de "
            f"{TAMANHO_MINIMO_SENHA} caracteres. Defina INVEST_SENHA com uma "
            "senha longa antes de subir a aplicação."
        )
    return usuario()


def _credencial_recebida(cabecalho: str | None) -> str | None:
    if not cabecalho:
        return None
    partes = cabecalho.split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "basic":
        return None
    try:
        bruto = base64.b64decode(partes[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return bruto.decode("utf-8")
    except UnicodeDecodeError:
        return None


def credencial_valida(cabecalho: str | None) -> bool:
    recebida = _credencial_recebida(cabecalho)
    if recebida is None:
        return False
    senha = _senha()
    if senha is None or len(senha) < TAMANHO_MINIMO_SENHA:
        return False
    esperada = f"{usuario()}:{senha}"
    return secrets.compare_digest(
        recebida.encode("utf-8"), esperada.encode("utf-8")
    )


def _host(valor: str | None) -> str | None:
    """Extrai o host de um ``Host``/``Origin``/``Referer``, sem esquema nem porta."""

    if not valor:
        return None
    texto = valor.strip()
    if not texto:
        return None
    if "://" not in texto:
        texto = f"//{texto}"
    try:
        host = urlsplit(texto).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def origem_confiavel(request: Request) -> bool:
    """Confere ``Origin`` (ou ``Referer``) contra o ``Host`` da requisição.

    HTTP Basic viaja junto com qualquer requisição à origem, então um POST de
    outro site chegaria autenticado. Exigir que a origem declarada seja a mesma
    fecha essa fronteira; sem ``Origin`` nem ``Referer`` a escrita é recusada.
    """

    esperado = _host(request.headers.get("host"))
    if esperado is None:
        return False
    origem = request.headers.get("origin")
    if origem is not None:
        return _host(origem) == esperado
    return _host(request.headers.get("referer")) == esperado


class AutenticacaoMiddleware(BaseHTTPMiddleware):
    """Exige HTTP Basic em todas as rotas, inclusive API e robots.txt."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not credencial_valida(request.headers.get("authorization")):
            return PlainTextResponse(
                "Autenticação necessária.\n",
                status_code=401,
                headers={"WWW-Authenticate": DESAFIO_BASIC},
            )
        if request.method in METODOS_QUE_ALTERAM and not origem_confiavel(request):
            return PlainTextResponse(
                "Origem da requisição não confiável.\n", status_code=403
            )
        return await call_next(request)


class NaoIndexarMiddleware(BaseHTTPMiddleware):
    """Acrescenta o cabeçalho de não indexação em toda resposta."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        resposta = await call_next(request)
        resposta.headers["X-Robots-Tag"] = CABECALHO_ROBOTS
        return resposta
