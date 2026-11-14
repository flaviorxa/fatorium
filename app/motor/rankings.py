"""Geração e comparação de rankings, com histórico preservado no banco."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from app import config as app_config
from app import db
from app.motor import estrategias, tecnicos
from app.motor.indicadores import (
    avaliar_fundamentalista,
    regularidade_proventos_24m,
    retorno_total_12m,
)

ESTRATEGIAS_ACOES = (
    "magic_formula",
    "earnings_yield",
    "upside_consenso",
    "pl_roe",
    "graham",
    "bazin",
    "dividendos",
    "qualidade",
)
ALIASES_CLASSE = {
    "qualidade_crescimento": "qualidade",
    "trend": "trend_template",
    "forca-relativa": "forca_relativa",
    "magic_formula_ev_ebit": "magic_formula",
}
ORDEM = (
    "magic_formula",
    "earnings_yield",
    "upside_consenso",
    "pl_roe",
    "graham",
    "bazin",
    "dividendos",
    "regularidade_proventos",
    "qualidade",
    "trend_template",
    "fiis",
    "fiis_ltm",
    "etfs",
)


def estrategias_ativas() -> tuple[str, ...]:
    """Estratégias exibidas nas listagens, na ordem canônica."""

    return ORDEM


def _agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(valor: Any) -> str:
    return json.dumps(valor, ensure_ascii=False, default=str)


def _numero(valor: Any) -> float | None:
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if pd.isna(numero):
        return None
    return numero


def _campo(valor: Any) -> float | str | None:
    """Número quando possível; texto preservado (ex.: segmento_cvm)."""

    numero = _numero(valor)
    if numero is not None:
        return numero
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _carregar_ativos() -> list[dict[str, Any]]:
    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT ticker, nome, classe, setor, moeda FROM ativos "
            "WHERE ativo = 1 ORDER BY ticker"
        ).fetchall()

    if linhas:
        return [dict(linha) for linha in linhas]

    try:
        universo = app_config.ler_universo()
    except FileNotFoundError:
        return []
    return [
        {"ticker": ticker, "nome": None, "classe": classe, "setor": None, "moeda": None}
        for ticker, classe in universo.items()
    ]


def _carregar_base() -> list[dict[str, Any]]:
    ativos = _carregar_ativos()
    bases: list[dict[str, Any]] = []
    limite = date.today() - timedelta(days=365)

    with db.conectar() as conn:
        for ativo in ativos:
            ticker = ativo["ticker"]
            cotacoes = conn.execute(
                """
                SELECT data, abertura, maxima, minima, fechamento, volume
                  FROM cotacoes
                 WHERE ticker = ?
                 ORDER BY data
                """,
                (ticker,),
            ).fetchall()
            df = pd.DataFrame([dict(linha) for linha in cotacoes])

            referencia = conn.execute(
                "SELECT MAX(data_ref) FROM fundamentos WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
            campos: dict[str, Any] = {}
            if referencia is not None:
                linhas = conn.execute(
                    "SELECT campo, valor FROM fundamentos WHERE ticker = ? "
                    "ORDER BY data_ref, rowid",
                    (ticker,),
                ).fetchall()
                for linha in linhas:
                    campos[linha["campo"]] = _campo(linha["valor"])

            proventos = [
                {"data_ex": linha["data_ex"], "valor": _numero(linha["valor"])}
                for linha in conn.execute(
                    "SELECT data_ex, valor FROM proventos "
                    "WHERE ticker = ? AND fonte = 'yahoo' ORDER BY data_ex",
                    (ticker,),
                ).fetchall()
            ]

            proventos_12m = 0.0
            tem_provento_12m = False
            for item in proventos:
                valor = item["valor"]
                data_ex = item["data_ex"]
                if valor is None or data_ex is None:
                    continue
                try:
                    quando = date.fromisoformat(str(data_ex)[:10])
                except ValueError:
                    continue
                if quando >= limite:
                    proventos_12m += valor
                    tem_provento_12m = True

            retorno = retorno_total_12m(
                df.to_dict("records") if not df.empty else [],
                proventos,
            )

            preco_cotacao = (
                _numero(df.iloc[-1]["fechamento"]) if not df.empty else None
            )
            indicadores = avaliar_fundamentalista(
                campos,
                ticker=ticker,
                proventos_12m=proventos_12m if tem_provento_12m else None,
                preco_fechamento=preco_cotacao,
                classe=ativo.get("classe"),
            )

            preco = None
            variacao = None
            liquidez_diaria_brl = _numero(campos.get("liquidez_diaria_brl"))
            if liquidez_diaria_brl is None:
                liquidez_diaria_brl = _numero(campos.get("liquidez_2meses"))
            if not df.empty:
                preco = _numero(df.iloc[-1]["fechamento"])
                if len(df) >= 2:
                    anterior = _numero(df.iloc[-2]["fechamento"])
                    if anterior is not None and anterior > 0 and preco is not None:
                        variacao = (preco / anterior - 1) * 100
                if liquidez_diaria_brl is None and "volume" in df.columns:
                    volumes = pd.to_numeric(df["volume"], errors="coerce").dropna()
                    if len(volumes):
                        liquidez_diaria_brl = float(volumes.iloc[-42:].mean())
            if preco is None:
                preco = _numero(campos.get("preco"))

            base: dict[str, Any] = {
                **campos,
                **regularidade_proventos_24m(proventos),
                **indicadores,
                **retorno,
                "ticker": ticker,
                "nome": ativo.get("nome") or campos.get("empresa"),
                "classe": ativo.get("classe"),
                "setor": ativo.get("setor") or campos.get("segmento_cvm"),
                "preco": preco,
                "variacao_pct": variacao,
                "liquidez_diaria_brl": liquidez_diaria_brl,
                "data_ref": referencia or (df.iloc[-1]["data"] if not df.empty else None),
                "proventos": proventos,
                "proventos_ja_por_acao": True,
                "cotacoes_df": df,
                "cotacoes": df["fechamento"].tolist() if not df.empty else [],
            }
            bases.append(base)

    return bases


def _principais(
    base: dict[str, Any],
    estrategia: str,
    dados: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comuns = (
        "pl",
        "pvp",
        "dy",
        "roe",
        "roic",
        "margem_liquida",
        "divida_liquida_ebitda",
        "crescimento_receita_5a",
    )
    especificos = {
        "magic_formula": ("earnings_yield", "return_on_capital", "ev_ebitda"),
        "earnings_yield": ("earnings_yield", "valor_firma"),
        "upside_consenso": (
            "alvo_medio",
            "upside_pct",
            "consenso_total",
            "data_consenso",
        ),
        "pl_roe": ("lpa", "vpa", "pl_x_roe"),
        "graham": ("valor_intrinseco_estimado", "divida_liquida_patrim", "liquidez_corrente"),
        "bazin": ("preco_teto", "dividendos_medios_por_acao"),
        "dividendos": ("payout",),
        "qualidade": ("ebitda", "ebitda_derivado"),
        "trend_template": ("sma50", "sma200"),
        "fiis": ("liquidez_diaria_brl", "pvp_origem"),
        "etfs": ("liquidez_diaria_brl", "retorno", "volatilidade"),
    }
    chaves = comuns + especificos.get(estrategia, ())
    valores = {chave: base.get(chave) for chave in chaves}
    # O campo cru pode faltar no ativo e a estratégia já ter resolvido o valor
    # (DY de FII sem Fundamentus = dy_12m_calculado). Sem isso o template lê
    # None e a home mostra N/A enquanto o ranking mostra o número.
    for chave, valor in valores.items():
        if valor is None and dados:
            valores[chave] = dados.get(chave)
    return valores


def _item(base: dict[str, Any], resultado: dict[str, Any], estrategia: str) -> dict[str, Any]:
    return {
        "ticker": base["ticker"],
        "nome": base.get("nome"),
        "classe": base.get("classe"),
        "setor": base.get("setor"),
        "preco": base.get("preco"),
        "variacao_pct": base.get("variacao_pct"),
        "indicadores": _principais(base, estrategia, resultado.get("dados")),
        "posicao": None,
        "pontuacao": resultado.get("pontuacao"),
        "elegivel": resultado.get("elegivel", False),
        "aprovados": resultado.get("aprovados", []),
        "reprovados": resultado.get("reprovados", []),
        "indisponiveis": resultado.get("indisponiveis", []),
        "motivos": resultado.get("motivos", {}),
        "data_ref": resultado.get("data_ref") or base.get("data_ref"),
        "dados": resultado.get("dados", {}),
        "destaque": resultado.get("destaque"),
    }


def _avaliar_trend(base: dict[str, Any]) -> dict[str, Any]:
    df = base.get("cotacoes_df")
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {
            "elegivel": False,
            "pontuacao": None,
            "aprovados": [],
            "reprovados": [],
            "indisponiveis": ["cotacoes"],
            "motivos": {"cotacoes": "sem histórico de cotações"},
            "data_ref": base.get("data_ref"),
            "dados": {},
        }

    template = tecnicos.trend_template(df)
    criterios = {
        chave: valor for chave, valor in template.items() if isinstance(valor, dict)
    }
    aprovados = [chave for chave, valor in criterios.items() if valor["atende"] is True]
    reprovados = [chave for chave, valor in criterios.items() if valor["atende"] is False]
    indisponiveis = [chave for chave, valor in criterios.items() if valor["atende"] is None]
    motivos = {
        chave: f"{valor['regra']} (valor: {valor['valor']})"
        for chave, valor in criterios.items()
    }
    disponiveis = template.get("criterios_disponiveis", 0) or 0
    atendidos = len(aprovados)
    if disponiveis >= 7:
        elegivel = atendidos >= 6
    else:
        elegivel = disponiveis > 0 and atendidos == disponiveis
    quase_todos = template.get("atende_todos") is False and atendidos >= 6
    motivo_falta = None
    if reprovados:
        faltante = reprovados[0]
        motivo_falta = f"{faltante}: {motivos.get(faltante, '')}".rstrip(": ")
    pontuacao = atendidos / disponiveis * 100 if disponiveis else None
    return {
        "elegivel": elegivel,
        "pontuacao": pontuacao,
        "aprovados": aprovados,
        "reprovados": reprovados,
        "indisponiveis": indisponiveis,
        "motivos": motivos,
        "data_ref": base.get("data_ref"),
        "dados": {k: v for k, v in template.items() if not isinstance(v, dict)}
        | {
            "sma50": criterios.get("preco_acima_sma50", {}).get("valor"),
            "sma200": criterios.get("preco_acima_sma200", {}).get("valor"),
            "distancia_maxima_52s": criterios.get("proximo_maxima_52s", {}).get(
                "valor"
            ),
            "criterios_atendidos": atendidos,
            "quase_todos": quase_todos,
            "motivo_falta": motivo_falta,
        },
    }


SEMANTICA_PONTUACAO = {
    "magic_formula": "soma de postos, menor é melhor",
    "earnings_yield": "maior é melhor",
    "upside_consenso": "upside até o preço-alvo (maior é melhor)",
    "pl_roe": "soma de postos, menor é melhor",
    "graham": "maior é melhor",
    "bazin": "maior é melhor",
    "dividendos": "maior é melhor",
    "regularidade_proventos": "meses com provento em 24 (maior é melhor)",
    "qualidade": "maior é melhor",
    "trend_template": "maior é melhor",
    "fiis": "maior é melhor",
    "fiis_ltm": "retorno total 12m (maior é melhor)",
    "etfs": "maior é melhor",
    "consolidado": "média dos percentis de posição × 100, maior é melhor",
}

DESEMPATE = {
    "magic_formula": "return_on_capital maior e, por último, ticker",
    "earnings_yield": "valor_firma menor e, por último, ticker",
    "upside_consenso": "consenso_total maior e, por último, ticker",
    "pl_roe": "roe maior e, por último, ticker",
    "graham": "pl_pvp menor e, por último, ticker",
    "bazin": "mediana de dividendos maior e, por último, ticker",
    "dividendos": "dy maior e, por último, ticker",
    "regularidade_proventos": "pagamentos em 24m desc e, por último, ticker",
    "qualidade": "roic maior e, por último, ticker",
    "trend_template": "distancia_maxima_52s menor e, por último, ticker",
    "fiis": "dy maior e, por último, ticker",
    "fiis_ltm": "dy maior e, por último, ticker",
    "etfs": "liquidez maior e, por último, ticker",
    "consolidado": "listas desc, pontuação desc e, por último, ticker",
}


def _ordem_menor(valor: Any) -> tuple[int, float]:
    numero = _numero(valor)
    return (1, 0.0) if numero is None else (0, numero)


def _ordem_maior(valor: Any) -> tuple[int, float]:
    numero = _numero(valor)
    return (1, 0.0) if numero is None else (0, -numero)


def _chave_merito(estrategia: str, item: dict[str, Any]) -> tuple[Any, ...]:
    dados = item.get("dados") or {}
    if estrategia == "graham":
        return (_ordem_menor(dados.get("pl_pvp")),)
    if estrategia == "bazin":
        return (_ordem_maior(dados.get("dividendos_medios_por_acao")),)
    if estrategia == "trend_template":
        return (_ordem_menor(dados.get("distancia_maxima_52s")),)
    if estrategia == "fiis":
        return (_ordem_maior(dados.get("dy")),)
    if estrategia == "fiis_ltm":
        return (_ordem_maior(dados.get("dy")),)
    if estrategia == "dividendos":
        return (_ordem_maior(dados.get("dy")),)
    if estrategia == "regularidade_proventos":
        return (_ordem_maior(dados.get("proventos_24m_qtd")),)
    if estrategia == "qualidade":
        return (_ordem_maior(dados.get("roic")),)
    if estrategia == "etfs":
        return (_ordem_maior(dados.get("liquidez")),)
    return ()


def _motivo_fora(item: dict[str, Any]) -> str:
    motivos = item.get("motivos") or {}
    for chave in list(item.get("reprovados") or []) + list(
        item.get("indisponiveis") or []
    ):
        if chave in motivos:
            return str(motivos[chave])
    if item.get("reprovados"):
        return "reprovado: " + ", ".join(item["reprovados"])
    if item.get("indisponiveis"):
        return "sem dado: " + ", ".join(item["indisponiveis"])
    return "inelegível"


def _entrada_fora(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": item["ticker"],
        "motivo": _motivo_fora(item),
        "dados": item.get("dados") or {},
    }


def _ordenar(
    estrategia: str, itens: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if estrategia == "magic_formula":
        elegiveis = [
            item
            for item in itens
            if item["elegivel"]
            and item.get("dados", {}).get("earnings_yield") is not None
            and item.get("dados", {}).get("return_on_capital") is not None
        ]
    elif estrategia == "earnings_yield":
        elegiveis = [
            item
            for item in itens
            if item["elegivel"]
            and item.get("dados", {}).get("earnings_yield") is not None
        ]
    elif estrategia == "upside_consenso":
        elegiveis = [
            item
            for item in itens
            if item["elegivel"]
            and item.get("dados", {}).get("upside_pct") is not None
        ]
    elif estrategia == "pl_roe":
        elegiveis = [
            item
            for item in itens
            if item["elegivel"]
            and item.get("dados", {}).get("pl") is not None
            and item.get("dados", {}).get("roe") is not None
        ]
    else:
        elegiveis = [item for item in itens if item["elegivel"]]

    if estrategia == "magic_formula" and elegiveis:
        postos: dict[str, dict[str, int]] = {}
        for campo, chave in (
            ("earnings_yield", "posto_ey"),
            ("return_on_capital", "posto_roc"),
        ):
            ordenados = sorted(
                elegiveis,
                key=lambda item, campo=campo: item["dados"][campo],
                reverse=True,
            )
            for posicao, item in enumerate(ordenados, start=1):
                postos.setdefault(item["ticker"], {})[chave] = posicao

        for item in elegiveis:
            valores = postos[item["ticker"]]
            soma_postos = valores["posto_ey"] + valores["posto_roc"]
            item["dados"]["posto_ey"] = valores["posto_ey"]
            item["dados"]["posto_roc"] = valores["posto_roc"]
            item["dados"]["soma_postos"] = soma_postos
            item["pontuacao"] = float(soma_postos)

        elegiveis.sort(
            key=lambda item: (
                item["dados"]["soma_postos"],
                -item["dados"]["return_on_capital"],
                item["ticker"],
            )
        )
    elif estrategia == "earnings_yield" and elegiveis:
        for item in elegiveis:
            item["pontuacao"] = item["dados"]["earnings_yield"] * 100
        elegiveis.sort(
            key=lambda item: (
                -item["dados"]["earnings_yield"],
                _ordem_menor(item["dados"].get("valor_firma")),
                item["ticker"],
            )
        )
    elif estrategia == "upside_consenso" and elegiveis:
        for item in elegiveis:
            item["pontuacao"] = float(item["dados"]["upside_pct"])
        elegiveis.sort(
            key=lambda item: (
                -item["dados"]["upside_pct"],
                -(_numero(item["dados"].get("consenso_total")) or 0.0),
                item["ticker"],
            )
        )
    elif estrategia == "pl_roe" and elegiveis:
        postos: dict[str, dict[str, int]] = {}
        for campo, chave, crescente in (
            ("pl", "posto_pl", True),
            ("roe", "posto_roe", False),
        ):
            ordenados = sorted(
                elegiveis,
                key=lambda item, campo=campo: item["dados"][campo],
                reverse=not crescente,
            )
            for posicao, item in enumerate(ordenados, start=1):
                postos.setdefault(item["ticker"], {})[chave] = posicao

        for item in elegiveis:
            valores = postos[item["ticker"]]
            soma_postos = valores["posto_pl"] + valores["posto_roe"]
            item["dados"]["posto_pl"] = valores["posto_pl"]
            item["dados"]["posto_roe"] = valores["posto_roe"]
            item["dados"]["soma_postos"] = soma_postos
            item["pontuacao"] = float(soma_postos)

        elegiveis.sort(
            key=lambda item: (
                item["dados"]["soma_postos"],
                -item["dados"]["roe"],
                item["ticker"],
            )
        )
    elif estrategia == "graham":
        com_margem = [item for item in elegiveis if item["pontuacao"] is not None]
        sem_margem = [item for item in elegiveis if item["pontuacao"] is None]
        com_margem.sort(
            key=lambda item: (
                -item["pontuacao"],
                *_chave_merito(estrategia, item),
                item["ticker"],
            )
        )
        sem_margem.sort(
            key=lambda item: (*_chave_merito(estrategia, item), item["ticker"])
        )
        elegiveis = com_margem + sem_margem
    else:
        com_pontuacao = [item for item in elegiveis if item["pontuacao"] is not None]
        sem_pontuacao = [item for item in elegiveis if item["pontuacao"] is None]
        com_pontuacao.sort(
            key=lambda item: (
                -item["pontuacao"],
                *_chave_merito(estrategia, item),
                item["ticker"],
            )
        )
        sem_pontuacao.sort(key=lambda item: item["ticker"])
        elegiveis = com_pontuacao + sem_pontuacao

    for posicao, item in enumerate(elegiveis, start=1):
        item["posicao"] = posicao

    selecionados = {id(item) for item in elegiveis}
    fora = [_entrada_fora(item) for item in itens if id(item) not in selecionados]
    fora.sort(key=lambda entrada: entrada["ticker"])
    return elegiveis, fora


def _persistir(
    estrategia: str,
    itens: list[dict[str, Any]],
    fora: list[dict[str, Any]],
    config: dict[str, Any],
) -> int:
    config_ranking = dict(config)
    config_ranking["pontuacao"] = SEMANTICA_PONTUACAO[estrategia]
    config_ranking["desempate"] = DESEMPATE[estrategia]
    with db.conectar() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rankings (estrategia, gerado_em, config_json, universo_n)
            VALUES (?, ?, ?, ?)
            """,
            (estrategia, _agora(), _json(config_ranking), len(itens) + len(fora)),
        )
        ranking_id = int(cursor.lastrowid)
        for item in itens:
            dados = {chave: valor for chave, valor in item.items() if chave != "motivos"}
            conn.execute(
                """
                INSERT INTO ranking_itens (
                    ranking_id, ticker, posicao, pontuacao, dados_json,
                    motivos_json, fora_da_lista
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    ranking_id,
                    item["ticker"],
                    item.get("posicao"),
                    item.get("pontuacao"),
                    _json(dados),
                    _json(item.get("motivos", {})),
                ),
            )
        for entrada in fora:
            conn.execute(
                """
                INSERT INTO ranking_itens (
                    ranking_id, ticker, posicao, pontuacao, dados_json,
                    motivos_json, fora_da_lista
                ) VALUES (?, ?, NULL, NULL, ?, '{}', 1)
                """,
                (
                    ranking_id,
                    entrada["ticker"],
                    _json(entrada),
                ),
            )
    return ranking_id


def classes_da_estrategia(estrategia: str) -> set[str]:
    """Classes de ativo aceitas por cada estratégia (regra única)."""

    nome = ALIASES_CLASSE.get(str(estrategia or "").strip().lower())
    if nome is None:
        nome = str(estrategia or "").strip().lower()
    if nome in {"fiis", "fiis_ltm"}:
        return {"fii"}
    if nome == "regularidade_proventos":
        return {"acao_b3"}
    if nome == "etfs":
        return {"etf"}
    if nome in ESTRATEGIAS_ACOES:
        return {"acao_b3"}
    return {"acao_b3", "bdr"}


def _trend_classe_incompativel(base: dict[str, Any]) -> dict[str, Any]:
    motivo = "classe sem histórico compatível com o Trend Template"
    return {
        "elegivel": False,
        "pontuacao": None,
        "aprovados": [],
        "reprovados": ["classe"],
        "indisponiveis": [],
        "motivos": {"classe": motivo},
        "data_ref": base.get("data_ref"),
        "dados": {"classe": base.get("classe")},
    }


def gerar_rankings() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Gera, ordena e grava um novo ranking para cada estratégia.

    Cada estratégia devolve ``itens`` (somente elegíveis, com posição contígua)
    e ``fora_da_lista`` (inelegíveis, sem posição). O ranking anterior nunca é
    apagado: cada chamada cria uma nova linha em ``rankings`` com seus itens.
    """

    bases = _carregar_base()
    config = estrategias.CONFIG_PADRAO
    resultados: dict[str, list[dict[str, Any]]] = {nome: [] for nome in ORDEM}

    for base in bases:
        classe = base.get("classe")
        for nome in ESTRATEGIAS_ACOES:
            if classe not in classes_da_estrategia(nome) and classe != "bdr":
                continue
            if classe == "bdr" and nome == "upside_consenso":
                continue
            if classe == "bdr" and not estrategias.tem_fundamentos_empresa(base):
                resultado = {
                    "elegivel": False,
                    "pontuacao": None,
                    "aprovados": [],
                    "reprovados": [],
                    "indisponiveis": ["fundamentos_empresa_fonte"],
                    "motivos": {
                        "fundamentos_empresa_fonte": (
                            "BDR sem fundamentos da empresa-fonte; fica indisponível"
                        )
                    },
                    "data_ref": base.get("data_ref"),
                    "dados": {},
                }
            else:
                funcao = estrategias.ESTRATEGIAS[nome]
                resultado = funcao(base, config)
            resultados[nome].append(_item(base, resultado, nome))

        if classe in classes_da_estrategia("regularidade_proventos"):
            meses = _numero(base.get("regularidade_24m_meses")) or 0
            elegivel_reg = meses >= 1
            resultados["regularidade_proventos"].append(
                _item(
                    base,
                    {
                        "elegivel": elegivel_reg,
                        "pontuacao": base.get("regularidade_24m_pct")
                        if elegivel_reg
                        else None,
                        "aprovados": ["provento_24m"] if elegivel_reg else [],
                        "reprovados": [],
                        "indisponiveis": [] if elegivel_reg else ["provento_24m"],
                        "motivos": {
                            "provento_24m": f"{int(meses)} de 24 meses com pagamento"
                        }
                        if elegivel_reg
                        else {"provento_24m": "sem provento nos últimos 24 meses"},
                        "data_ref": base.get("data_ref"),
                        "dados": {
                            chave: base.get(chave)
                            for chave in (
                                "regularidade_24m_pct",
                                "regularidade_24m_meses",
                                "proventos_24m_qtd",
                                "proventos_24m_tipicos",
                                "proventos_24m_observacao",
                                "dy",
                            )
                        },
                    },
                    "regularidade_proventos",
                )
            )

        if classe in classes_da_estrategia("fiis"):
            resultado = estrategias.avaliar_fii(base, config)
            resultados["fiis"].append(_item(base, resultado, "fiis"))

            elegivel_ltm = base.get("retorno_total_12m_pct") is not None
            resultado_ltm = {
                "elegivel": elegivel_ltm,
                "pontuacao": base.get("retorno_total_12m_pct"),
                "aprovados": ["retorno_total_12m"] if elegivel_ltm else [],
                "reprovados": [],
                "indisponiveis": [] if elegivel_ltm else ["retorno_total_12m"],
                "motivos": {}
                if elegivel_ltm
                else {"retorno_total_12m": "sem cotação de 12 meses atrás"},
                "data_ref": base.get("data_ref"),
                "dados": {
                    chave: base.get(chave)
                    for chave in (
                        "retorno_total_12m_pct",
                        "valorizacao_12m_pct",
                        "proventos_12m_pct",
                        "proventos_12m",
                        "preco_inicial",
                        "data_inicial",
                        "dy",
                        "pvp",
                        "liquidez_diaria_brl",
                    )
                },
            }
            resultados["fiis_ltm"].append(
                _item(base, resultado_ltm, "fiis_ltm")
            )
        if classe in classes_da_estrategia("etfs"):
            resultado = estrategias.avaliar_etf(base, config)
            resultados["etfs"].append(_item(base, resultado, "etfs"))

        if classe in classes_da_estrategia("trend_template"):
            resultado_trend = _avaliar_trend(base)
        else:
            resultado_trend = _trend_classe_incompativel(base)
        resultados["trend_template"].append(
            _item(base, resultado_trend, "trend_template")
        )

    finais: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for nome in ORDEM:
        itens, fora = _ordenar(nome, resultados[nome])
        finais[nome] = {"itens": itens, "fora_da_lista": fora}
        _persistir(nome, itens, fora, config)

    from app.motor import consolidado

    itens_consolidado = []
    for posicao, item in enumerate(consolidado.consolidar(), start=1):
        itens_consolidado.append({**item, "posicao": posicao})
    _persistir(
        "consolidado",
        itens_consolidado,
        [],
        dict(consolidado.CONFIG_PADRAO),
    )

    return finais


def _ultimos_dois_ids(estrategia: str) -> list[int]:
    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT id FROM rankings WHERE estrategia = ? ORDER BY id DESC LIMIT 2",
            (estrategia,),
        ).fetchall()
    return [int(linha["id"]) for linha in linhas]


def _posicoes(ranking_id: int) -> dict[str, int]:
    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT ticker, posicao FROM ranking_itens WHERE ranking_id = ?",
            (ranking_id,),
        ).fetchall()
    return {
        linha["ticker"]: int(linha["posicao"])
        for linha in linhas
        if linha["posicao"] is not None
    }


def comparar_com_anterior(estrategia: str) -> dict[str, list[str]]:
    """Compara o último ranking com o penúltimo: entrou, saiu, subiu, desceu."""

    vazio = {"entrou": [], "saiu": [], "subiu": [], "desceu": []}
    ids = _ultimos_dois_ids(estrategia)
    if len(ids) < 2:
        return vazio

    atual = _posicoes(ids[0])
    anterior = _posicoes(ids[1])
    entrou = sorted(set(atual) - set(anterior))
    saiu = sorted(set(anterior) - set(atual))
    subiu = sorted(
        ticker for ticker in set(atual) & set(anterior) if atual[ticker] < anterior[ticker]
    )
    desceu = sorted(
        ticker for ticker in set(atual) & set(anterior) if atual[ticker] > anterior[ticker]
    )
    return {"entrou": entrou, "saiu": saiu, "subiu": subiu, "desceu": desceu}
