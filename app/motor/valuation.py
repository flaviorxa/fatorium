"""Preço-teto por método fundamentalista: Bazin e Graham.

Funções puras. Nenhuma inventa número: entrada ausente, zero ou negativa
devolve ``None`` e a tela mostra o motivo.
"""

from __future__ import annotations

import math
from typing import Any

# Bazin: o dividendo anual tem de render 6% do preço pago.
TAXA_BAZIN = 0.06
# Graham: preço-teto = raiz(22,5 x LPA x VPA).
MULTIPLO_GRAHAM = 22.5


def _positivo(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero) or numero <= 0:
        return None
    return numero


def bazin(dpa: Any, taxa: float = TAXA_BAZIN) -> float | None:
    """Preço-teto de Bazin: ``DPA / taxa`` (padrão 6% ao ano)."""

    dividendo = _positivo(dpa)
    if dividendo is None or taxa <= 0:
        return None
    return dividendo / taxa


def graham(lpa: Any, vpa: Any) -> float | None:
    """Preço-teto de Graham: ``raiz(22,5 x LPA x VPA)``."""

    lucro = _positivo(lpa)
    patrimonio = _positivo(vpa)
    if lucro is None or patrimonio is None:
        return None
    return math.sqrt(MULTIPLO_GRAHAM * lucro * patrimonio)


def margem(valor: Any, preco: Any) -> float | None:
    """Upside percentual do preço-teto até o preço atual."""

    alvo = _positivo(valor)
    atual = _positivo(preco)
    if alvo is None or atual is None:
        return None
    return (alvo / atual - 1) * 100
