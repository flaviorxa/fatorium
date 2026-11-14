"""Ranking consolidado: média dos percentis de posição entre estratégias.

Cada estratégia tem uma lista de tamanho próprio (ações, FIIs, ETFs). Comparar
posições absolutas entre listas diferentes não faz sentido, então a posição é
normalizada em percentil: ``(N - posicao + 1) / N``. O consolidado só entra com
ativos presentes em pelo menos ``min_listas`` listas.
"""

from __future__ import annotations

import json
from typing import Any

from app import db
from app.motor import rankings

CONFIG_PADRAO: dict[str, Any] = {"min_listas": 3}


def _config(config: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(CONFIG_PADRAO)
    if isinstance(config, dict):
        base.update(config)
    try:
        base["min_listas"] = max(1, int(base.get("min_listas", 3)))
    except (TypeError, ValueError):
        base["min_listas"] = CONFIG_PADRAO["min_listas"]
    return base


def _json(texto: Any) -> dict[str, Any]:
    if texto is None:
        return {}
    try:
        valor = json.loads(texto)
    except (TypeError, json.JSONDecodeError):
        return {}
    return valor if isinstance(valor, dict) else {}


def _indicadores(dados: dict[str, Any]) -> dict[str, Any]:
    origem = dados.get("indicadores") or {}
    if not isinstance(origem, dict):
        origem = {}
    return {
        chave: origem.get(chave) if origem.get(chave) is not None else dados.get(chave)
        for chave in ("pl", "pvp", "dy", "roe")
    }


def _ultimos_rankings() -> list[dict[str, Any]]:
    # ``fiis_ltm`` é ranking de desempenho: não deve contar como mais uma
    # lista para o ativo no consolidado.
    ativas = [a for a in rankings.estrategias_ativas() if a != "fiis_ltm"]
    if not ativas:
        return []
    marcadores = ", ".join("?" for _ in ativas)
    with db.conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT r.id, r.estrategia
              FROM rankings r
             WHERE r.estrategia IN ({marcadores})
               AND r.id IN (SELECT MAX(id) FROM rankings GROUP BY estrategia)
            """,
            ativas,
        ).fetchall()
    por_estrategia = {linha["estrategia"]: dict(linha) for linha in linhas}
    return [por_estrategia[nome] for nome in ativas if nome in por_estrategia]


def _itens_do_ranking(ranking_id: int) -> list[dict[str, Any]]:
    with db.conectar() as conn:
        linhas = conn.execute(
            """
            SELECT ticker, posicao, dados_json
              FROM ranking_itens
             WHERE ranking_id = ?
               AND fora_da_lista = 0
               AND posicao IS NOT NULL
               AND ticker NOT IN (
                   SELECT ticker FROM ativos WHERE ativo = 0
               )
             ORDER BY posicao, ticker
            """,
            (ranking_id,),
        ).fetchall()
    return [dict(linha) for linha in linhas]


def consolidar(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Devolve os ativos com a média dos percentis nas listas mais recentes."""

    configuracao = _config(config)
    minimo = int(configuracao["min_listas"])

    por_ticker: dict[str, dict[str, Any]] = {}
    for ranking in _ultimos_rankings():
        estrategia = ranking["estrategia"]
        itens = _itens_do_ranking(int(ranking["id"]))
        total = len(itens)
        if total == 0:
            continue

        for item in itens:
            ticker = str(item["ticker"]).strip().upper()
            if not ticker:
                continue
            try:
                posicao = int(item["posicao"])
            except (TypeError, ValueError):
                continue
            if posicao < 1 or posicao > total:
                continue

            percentil = (total - posicao + 1) / total
            dados = _json(item.get("dados_json"))
            acumulado = por_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "classe": None,
                    "nome": None,
                    "preco": None,
                    "data_ref": None,
                    "posicoes": {},
                    "percentis": [],
                    "_indicadores": {},
                },
            )

            acumulado["posicoes"][estrategia] = {
                "posicao": posicao,
                "de": total,
                "percentil": percentil,
            }
            acumulado["percentis"].append(percentil)

            for chave in ("classe", "nome", "preco", "data_ref"):
                if acumulado.get(chave) is None and dados.get(chave) is not None:
                    acumulado[chave] = dados.get(chave)
            for chave, valor in _indicadores(dados).items():
                if acumulado["_indicadores"].get(chave) is None and valor is not None:
                    acumulado["_indicadores"][chave] = valor

    resultado: list[dict[str, Any]] = []
    for acumulado in por_ticker.values():
        listas = len(acumulado["posicoes"])
        if listas < minimo:
            continue
        percentis = acumulado.pop("percentis")
        indicadores = acumulado.pop("_indicadores")
        acumulado["listas"] = listas
        acumulado["pontuacao"] = 100 * sum(percentis) / len(percentis)
        acumulado["indicadores"] = indicadores
        resultado.append(acumulado)

    resultado.sort(
        key=lambda item: (
            -item["listas"],
            -item["pontuacao"],
            item["ticker"],
        )
    )
    return resultado
