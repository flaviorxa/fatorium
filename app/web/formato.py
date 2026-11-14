"""Formatação brasileira para as páginas HTML (funções puras, sem I/O)."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

NA = "N/A"
MOTIVO_PADRAO = "valor indisponível na fonte"
FUSO_BR = ZoneInfo("America/Sao_Paulo")


def em_brasilia(valor: Any) -> Any:
    """Converte instantes com fuso para ``America/Sao_Paulo``.

    Datas e datetimes sem fuso e strings apenas de data são devolvidos
    inalterados. Strings ISO com fuso (inclusive sufixo ``Z``) viram
    ``datetime`` no fuso de Brasília.
    """

    if isinstance(valor, datetime):
        if valor.tzinfo is not None:
            return valor.astimezone(FUSO_BR)
        return valor
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return valor
        try:
            convertido = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            return valor
        if convertido.tzinfo is not None:
            return convertido.astimezone(FUSO_BR)
    return valor


def _numero(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto or texto.upper() in {"N/A", "NA", "-"}:
            return None
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            valor = float(texto)
        except ValueError:
            return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    return numero


def converter_numero(valor: Any) -> float | None:
    """Converte para float sem formatar (útil para comparar/ordenar)."""

    return _numero(valor)


def numero(valor: Any, casas: int = 2) -> str | None:
    """Número no padrão brasileiro: milhar com ponto, decimal com vírgula."""

    convertido = _numero(valor)
    if convertido is None:
        return None
    return f"{convertido:,.{casas}f}".replace(",", "\x00").replace(".", ",").replace(
        "\x00", "."
    )


def inteiro(valor: Any) -> str | None:
    return numero(valor, casas=0)


def moeda(valor: Any, casas: int = 2) -> str | None:
    """Valor monetário: ``R$ 1.234,56``."""

    texto = numero(valor, casas=casas)
    if texto is None:
        return None
    return f"R$ {texto}"


def percentual(valor: Any, casas: int = 2, sinal: bool = False) -> str | None:
    """Percentual com duas casas e vírgula: ``12,34%``."""

    texto = numero(valor, casas=casas)
    if texto is None:
        return None
    if sinal:
        convertido = _numero(valor)
        assert convertido is not None
        texto = ("+" if convertido > 0 else "") + texto
    return f"{texto}%"


def data_br(valor: Any) -> str | None:
    """Data ISO (ou datetime) no formato ``dd/mm/aaaa``, no fuso de Brasília."""

    if valor is None:
        return None
    convertido = em_brasilia(valor)
    if isinstance(convertido, datetime):
        return convertido.strftime("%d/%m/%Y")
    if isinstance(convertido, date):
        return convertido.strftime("%d/%m/%Y")
    texto = str(convertido).strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return texto


def hora_br(valor: Any) -> str | None:
    """Hora de um instante ISO no formato ``HH:MM``, no fuso de Brasília."""

    if valor is None:
        return None
    convertido = em_brasilia(valor)
    if isinstance(convertido, datetime):
        return convertido.strftime("%H:%M")
    texto = str(convertido).strip()
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return None


def celula(
    valor: Any,
    tipo: str = "numero",
    casas: int = 2,
    motivo: str | None = None,
    sinal: bool = False,
) -> dict[str, str | None]:
    """Monta uma célula de tabela já formatada, com o motivo do ``N/A``."""

    if tipo == "texto":
        texto = None if valor is None or str(valor).strip() == "" else str(valor)
    elif tipo == "data":
        texto = data_br(valor)
    elif tipo == "inteiro":
        texto = inteiro(valor)
    elif tipo == "moeda":
        texto = moeda(valor, casas=casas)
    elif tipo == "percentual":
        texto = percentual(valor, casas=casas, sinal=sinal)
    else:
        texto = numero(valor, casas=casas)

    if texto is None:
        return {"texto": NA, "motivo": motivo or MOTIVO_PADRAO}
    return {"texto": texto, "motivo": None}
