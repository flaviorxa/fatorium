"""Consultas de leitura usadas pelos templates HTML."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from app import config, db
from app.fontes import msn
from app.motor import rankings
from app.motor.indicadores import avaliar_fundamentalista, retorno_total_12m
from app.web import formato

NOMES_ESTRATEGIAS = {
    "magic_formula": "Magic Formula",
    "earnings_yield": "Earnings Yield (EBIT/EV)",
    "upside_consenso": "Maior upside (consenso MSN)",
    "pl_roe": "P/L × ROE (financeiras incluídas)",
    "graham": "Graham",
    "bazin": "Bazin",
    "dividendos": "Dividendos",
    "regularidade_proventos": "Regularidade de proventos (24m)",
    "qualidade": "Qualidade e crescimento",
    "trend_template": "Trend Template",
    "fiis": "Fundos imobiliários",
    "fiis_ltm": "FIIs por retorno total (12m)",
    "etfs": "ETFs",
}

NOMES_CURTOS_ESTRATEGIAS = {
    "magic_formula": "Magic",
    "earnings_yield": "Earnings yield",
    "upside_consenso": "Maior upside",
    "pl_roe": "P/L × ROE",
    "graham": "Graham",
    "bazin": "Bazin",
    "dividendos": "Dividendos",
    "regularidade_proventos": "Regularidade 24m",
    "qualidade": "Qualidade",
    "trend_template": "Trend",
    "fiis": "FIIs",
    "fiis_ltm": "FIIs 12m",
    "etfs": "ETFs",
}

CLASSES = {
    "acao_b3": "Ação B3",
    "bdr": "BDR",
    "fii": "FII",
    "etf": "ETF",
    "cripto": "Cripto",
    "indice": "Índice",
}

CAMPOS_CONSENSO = (
    "consenso_total",
    "consenso_compra",
    "consenso_neutro",
    "consenso_venda",
    "recomendacao",
    "taxa_recomendacao",
    "alvo_medio",
    "alvo_mediana",
    "alvo_max",
    "alvo_min",
)


def consensos_recentes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Último consenso do MSN por ticker, em uma consulta só.

    O consenso fica em ``fundamentos`` com fonte ``msn``. A janela é por
    campo, como em ``fundamentos_mais_recentes``: cada campo pode ter sido
    coletado em uma data diferente.
    """

    lista = sorted({str(t).strip().upper() for t in tickers if t and str(t).strip()})
    if not lista:
        return {}

    marcadores = ", ".join("?" for _ in lista)
    campos = ", ".join("?" for _ in CAMPOS_CONSENSO)
    with db.conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT ticker, campo, valor, data_ref
              FROM fundamentos
             WHERE fonte = 'msn'
               AND ticker IN ({marcadores})
               AND campo IN ({campos})
            """,
            (*lista, *CAMPOS_CONSENSO),
        ).fetchall()

    resultado: dict[str, dict[str, Any]] = {}
    escolhido: dict[tuple[str, str], tuple[str, str]] = {}
    for linha in linhas:
        ticker = str(linha["ticker"] or "").strip().upper()
        campo = str(linha["campo"])
        data_ref = str(linha["data_ref"] or "")
        chave = (ticker, campo)
        if chave not in escolhido or data_ref >= escolhido[chave][0]:
            escolhido[chave] = (data_ref, data_ref)
            resultado.setdefault(ticker, {})[campo] = linha["valor"]
            datas = resultado[ticker].setdefault("consenso_datas", [])
            datas.append(data_ref)

    for dados in resultado.values():
        datas = dados.pop("consenso_datas", [])
        if datas:
            dados["consenso_data_ref"] = max(datas)
    return resultado


def _anexar_consenso(itens: list[dict[str, Any]]) -> None:
    consensos = consensos_recentes([str(item.get("ticker") or "") for item in itens])
    for item in itens:
        dados = consensos.get(str(item.get("ticker") or "").strip().upper())
        if dados:
            item.update(dados)


def consenso_resumo(dados: dict[str, Any] | None) -> dict[str, str | None] | None:
    """Resumo curto do consenso para tabelas e cabeçalho do ativo.

    Aceita tanto o item do ranking (campos no topo ou em ``dados``) quanto o
    dicionário de campos do MSN. Devolve ``None`` quando não há consenso.
    """

    if not isinstance(dados, dict):
        return None

    def numero(chave: str) -> float | None:
        return formato.converter_numero(valor_item(dados, chave))

    compra = numero("consenso_compra")
    neutro = numero("consenso_neutro")
    venda = numero("consenso_venda")
    total = numero("consenso_total")
    if total is None and None not in (compra, neutro, venda):
        total = compra + neutro + venda
    if (
        total is None
        or total <= 0
        or compra is None
        or neutro is None
        or venda is None
    ):
        return None

    texto = " · ".join(
        f"{formato.inteiro(valor)}{sufixo}"
        for valor, sufixo in ((compra, "C"), (neutro, "N"), (venda, "V"))
    )

    alvo_texto = None
    alvo = numero("alvo_medio")
    alvo_duvidoso = False
    if alvo is not None:
        alvo_texto = formato.moeda(alvo)
        preco = numero("preco")
        if preco is None:
            preco = numero("fechamento")
        alvo_duvidoso = msn.alvo_classe_duvidosa(alvo, preco)
        if alvo_duvidoso:
            alvo_texto = (
                f"{alvo_texto} · classe em dúvida "
                "(o alvo pode ser da unit/outra classe do papel)"
            )
        else:
            upside = msn.upside_pct(alvo, preco)
            if upside is not None and alvo_texto:
                alvo_texto = (
                    f"{alvo_texto} · "
                    f"{formato.percentual(upside, casas=1, sinal=True)}"
                )

    data = formato.data_br(
        valor_item(dados, "consenso_data_ref") or valor_item(dados, "data_ref")
    )
    return {
        "texto": texto,
        "alvo_texto": alvo_texto,
        "alvo_classe_duvidosa": alvo_duvidoso,
        "data": data,
    }


def _dict(linha: Any) -> dict[str, Any]:
    return {chave: linha[chave] for chave in linha.keys()}


def _json(texto: Any, padrao: Any) -> Any:
    if texto is None:
        return padrao
    try:
        valor = json.loads(texto)
    except (TypeError, json.JSONDecodeError):
        return padrao
    return valor if isinstance(valor, type(padrao)) else padrao


def nome_estrategia(chave: str) -> str:
    return NOMES_ESTRATEGIAS.get(chave, chave.replace("_", " ").title())


def nome_curto_estrategia(chave: str) -> str:
    return NOMES_CURTOS_ESTRATEGIAS.get(chave, nome_estrategia(chave))


def nome_classe(chave: str | None) -> str:
    if not chave:
        return "N/A"
    return CLASSES.get(chave, chave)


def carregar_ativo(ticker: str) -> dict[str, Any] | None:
    ticker = ticker.strip().upper()
    with db.conectar() as conn:
        ativo = conn.execute(
            "SELECT * FROM ativos WHERE ticker = ?", (ticker,)
        ).fetchone()
        if ativo is None:
            return None

        cotacoes = conn.execute(
            """
            SELECT data, abertura, maxima, minima, fechamento,
                   fechamento_ajustado, volume, fonte
              FROM cotacoes
             WHERE ticker = ?
             ORDER BY data DESC
             LIMIT 800
            """,
            (ticker,),
        ).fetchall()
        cotacoes = list(reversed(cotacoes))

        proventos = conn.execute(
            """
            SELECT data_ex, valor, tipo, fonte
              FROM proventos
             WHERE ticker = ?
             ORDER BY data_ex DESC
            """,
            (ticker,),
        ).fetchall()

    detalhes = db.fundamentos_mais_recentes(ticker)
    campos = {campo: info["valor"] for campo, info in detalhes.items()}
    datas_fundamentos = [
        str(info["data_ref"])
        for info in detalhes.values()
        if info.get("data_ref")
    ]
    referencia = max(datas_fundamentos) if datas_fundamentos else None
    fontes_fundamentos = sorted(
        {str(info["fonte"]) for info in detalhes.values() if info.get("fonte")}
    )

    hoje = date.today()
    limite = hoje - timedelta(days=365)
    proventos_12m = 0.0
    tem_provento = False
    for item in proventos:
        try:
            quando = date.fromisoformat(str(item["data_ex"])[:10])
        except (TypeError, ValueError):
            continue
        if quando >= limite and item["valor"] is not None:
            proventos_12m += float(item["valor"])
            tem_provento = True

    ultima = _dict(cotacoes[-1]) if cotacoes else None
    anterior = _dict(cotacoes[-2]) if len(cotacoes) >= 2 else None
    variacao = None
    if (
        ultima
        and anterior
        and anterior.get("fechamento")
        and ultima.get("fechamento") is not None
    ):
        variacao = (ultima["fechamento"] / anterior["fechamento"] - 1) * 100

    indicadores = avaliar_fundamentalista(
        campos,
        ticker=ticker,
        proventos_12m=proventos_12m if tem_provento else None,
        preco_fechamento=ultima.get("fechamento") if ultima else None,
        classe=ativo["classe"],
    )

    return {
        "ativo": _dict(ativo),
        "cotacoes": [_dict(linha) for linha in cotacoes],
        "ultima": ultima,
        "anterior": anterior,
        "variacao_pct": variacao,
        "fundamentos": {
            "data_ref": referencia,
            "fontes": fontes_fundamentos,
            "campos": campos,
            "detalhes": detalhes,
        },
        "proventos": [_dict(linha) for linha in proventos],
        "proventos_12m": proventos_12m if tem_provento else None,
        "indicadores": indicadores,
        "retorno_12m": retorno_total_12m(
            [_dict(linha) for linha in cotacoes],
            [_dict(linha) for linha in proventos],
        ),
    }


def listar_ativos(
    *,
    classe: str | None = None,
    setor: str | None = None,
    busca: str | None = None,
    ordem: str = "ticker",
    direcao: str = "asc",
    pagina: int = 1,
    por_pagina: int = 50,
) -> dict[str, Any]:
    ordens = {
        "ticker": "a.ticker",
        "nome": "a.nome",
        "classe": "a.classe",
        "setor": "a.setor",
        "ultima_data": "c.data",
        "fechamento": "c.fechamento",
        "dy": "CAST(f.valor AS REAL)",
    }
    ordem = ordem if ordem in ordens else "ticker"
    direcao = "desc" if str(direcao).lower() == "desc" else "asc"
    pagina = max(1, int(pagina))

    filtros = ["1 = 1"]
    parametros: list[Any] = []
    if classe:
        filtros.append("a.classe = ?")
        parametros.append(classe)
    if setor:
        filtros.append("a.setor = ?")
        parametros.append(setor)
    if busca:
        filtros.append(
            "(LOWER(a.ticker) LIKE ? OR LOWER(COALESCE(a.nome, '')) LIKE ?)"
        )
        termo = f"%{busca.lower()}%"
        parametros.extend([termo, termo])
    onde = " AND ".join(filtros)

    base = f"""
        FROM ativos a
        LEFT JOIN cotacoes c
          ON c.ticker = a.ticker
         AND c.data = (SELECT MAX(data) FROM cotacoes x WHERE x.ticker = a.ticker)
        LEFT JOIN fundamentos f
          ON f.ticker = a.ticker
         AND f.campo = 'dy'
         AND f.data_ref = (SELECT MAX(data_ref) FROM fundamentos y
                            WHERE y.ticker = a.ticker AND y.campo = 'dy')
        WHERE {onde}
    """
    with db.conectar() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) {base}", parametros).fetchone()[0])
        linhas = conn.execute(
            f"""
            SELECT a.ticker, a.nome, a.classe, a.setor, a.moeda, a.fonte,
                   a.atualizado_em, c.data AS ultima_data,
                   c.fechamento, c.volume, f.valor AS dy,
                   (SELECT x.fechamento
                      FROM cotacoes x
                     WHERE x.ticker = a.ticker
                     ORDER BY x.data DESC
                     LIMIT 1 OFFSET 1) AS fechamento_anterior
            {base}
            ORDER BY {ordens[ordem]} {direcao.upper()}, a.ticker
            LIMIT ? OFFSET ?
            """,
            [*parametros, por_pagina, (pagina - 1) * por_pagina],
        ).fetchall()

    itens = []
    for linha in linhas:
        item = _dict(linha)
        anterior = item.pop("fechamento_anterior", None)
        if anterior and item["fechamento"] is not None:
            item["variacao_pct"] = (item["fechamento"] / anterior - 1) * 100
        else:
            item["variacao_pct"] = None
        itens.append(item)

    _anexar_consenso(itens)

    return {
        "itens": itens,
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": max(1, (total + por_pagina - 1) // por_pagina),
    }


def opcoes_filtros() -> dict[str, list[str]]:
    with db.conectar() as conn:
        classes = [
            linha["classe"]
            for linha in conn.execute(
                "SELECT DISTINCT classe FROM ativos WHERE classe IS NOT NULL ORDER BY classe"
            ).fetchall()
        ]
        setores = [
            linha["setor"]
            for linha in conn.execute(
                "SELECT DISTINCT setor FROM ativos WHERE setor IS NOT NULL ORDER BY setor"
            ).fetchall()
        ]
    return {"classes": classes, "setores": setores}


def rankings_recentes() -> list[dict[str, Any]]:
    ativas = rankings.estrategias_ativas()
    marcadores = ", ".join("?" for _ in ativas)
    with db.conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT r.id, r.estrategia, r.gerado_em, r.universo_n, r.config_json,
                   (SELECT COUNT(*) FROM ranking_itens i
                     WHERE i.ranking_id = r.id AND i.fora_da_lista = 0) AS itens
              FROM rankings r
             WHERE r.estrategia IN ({marcadores})
               AND r.id IN (SELECT MAX(id) FROM rankings GROUP BY estrategia)
             ORDER BY r.estrategia
            """,
            ativas,
        ).fetchall()
    por_nome = {linha["estrategia"]: _dict(linha) for linha in linhas}
    resultado = []
    for estrategia in ativas:
        resultado.append(
            por_nome.pop(
                estrategia,
                {
                    "id": None,
                    "estrategia": estrategia,
                    "gerado_em": None,
                    "universo_n": None,
                    "itens": 0,
                    "config_json": None,
                },
            )
        )
    return resultado


def carregar_ranking(estrategia: str) -> dict[str, Any] | None:
    with db.conectar() as conn:
        ranking = conn.execute(
            "SELECT * FROM rankings WHERE estrategia = ? ORDER BY id DESC LIMIT 1",
            (estrategia,),
        ).fetchone()
        if ranking is None:
            return None
        itens = conn.execute(
            """
            SELECT ticker, posicao, pontuacao, dados_json, motivos_json,
                   fora_da_lista
              FROM ranking_itens
             WHERE ranking_id = ?
             ORDER BY fora_da_lista, COALESCE(posicao, 999999), ticker
            """,
            (ranking["id"],),
        ).fetchall()

    lista: list[dict[str, Any]] = []
    fora: list[dict[str, Any]] = []
    for item in itens:
        dados = _json(item["dados_json"], {})
        if item["fora_da_lista"]:
            fora.append(
                {
                    "ticker": item["ticker"],
                    "motivo": dados.get("motivo", "inelegível"),
                    "dados": dados.get("dados") or {},
                }
            )
            continue
        dados["motivos"] = _json(item["motivos_json"], {})
        dados["ticker"] = item["ticker"]
        dados["posicao"] = item["posicao"]
        dados["pontuacao"] = item["pontuacao"]
        lista.append(dados)

    _anexar_consenso(lista)

    return {
        "id": ranking["id"],
        "estrategia": ranking["estrategia"],
        "gerado_em": ranking["gerado_em"],
        "universo_n": ranking["universo_n"],
        "config": _json(ranking["config_json"], {}),
        "itens": lista,
        "fora_da_lista": fora,
    }


def rankings_do_ticker(ticker: str) -> list[dict[str, Any]]:
    ticker = ticker.strip().upper()
    ativas = rankings.estrategias_ativas()
    marcadores = ", ".join("?" for _ in ativas)
    with db.conectar() as conn:
        linhas = conn.execute(
            f"""
            SELECT r.estrategia, i.ticker, i.posicao, i.pontuacao,
                   i.dados_json, i.motivos_json, i.fora_da_lista
              FROM rankings r
              JOIN ranking_itens i ON i.ranking_id = r.id
             WHERE r.estrategia IN ({marcadores})
               AND r.id IN (SELECT MAX(id) FROM rankings GROUP BY estrategia)
               AND i.ticker = ?
            """,
            (*ativas, ticker),
        ).fetchall()
    por_estrategia = {linha["estrategia"]: linha for linha in linhas}
    resultado = []
    for estrategia in ativas:
        linha = por_estrategia.get(estrategia)
        if linha is None:
            resultado.append(
                {
                    "estrategia": estrategia,
                    "nome": nome_estrategia(estrategia),
                    "avaliado": False,
                }
            )
            continue
        dados = _json(linha["dados_json"], {})
        motivos = _json(linha["motivos_json"], {})
        resultado.append(
            {
                "estrategia": estrategia,
                "nome": nome_estrategia(estrategia),
                "avaliado": True,
                "fora_da_lista": bool(linha["fora_da_lista"]),
                "posicao": linha["posicao"],
                "pontuacao": linha["pontuacao"],
                "motivo_fora": dados.get("motivo"),
                "aprovados": dados.get("aprovados") or [],
                "reprovados": dados.get("reprovados") or [],
                "indisponiveis": dados.get("indisponiveis") or [],
                "motivos": motivos,
                "dados": dados.get("dados") or {},
                "destaque": dados.get("destaque"),
            }
        )
    return resultado


def ultimos_alertas(limite: int = 8) -> list[dict[str, Any]]:
    with db.conectar() as conn:
        linhas = conn.execute(
            """
            SELECT quando, fonte, ticker, etapa, erro
              FROM falhas
             ORDER BY id DESC
             LIMIT ?
            """,
            (limite,),
        ).fetchall()
    return [_dict(linha) for linha in linhas]


def mapeamento_fii() -> dict[str, Any]:
    """Pendências de CNPJ calculadas do estado atual, não do histórico.

    ``tickers_pendentes`` é a diferença entre os FIIs do universo e as chaves
    do mapa ticker–CNPJ. Falhas antigas de mapeamento não entram na conta.
    """

    try:
        universo = config.ler_universo()
    except FileNotFoundError:
        universo = {}
    mapa = config.ler_mapa_fii()
    pendentes = sorted(
        ticker
        for ticker, classe in universo.items()
        if classe == "fii" and ticker not in mapa
    )
    if pendentes:
        texto = (
            f"{len(pendentes)} FII(s) sem CNPJ: "
            "use scripts/coletar_cnpj_clubefii.py"
        )
    else:
        texto = "todos os FIIs do universo têm CNPJ mapeado"
    return {
        "entradas": len(mapa),
        "revisao_manual": len(mapa) < 15,
        "tickers_pendentes": pendentes,
        "pendentes": len(pendentes),
        "texto": texto,
    }


def resumo_dashboard() -> dict[str, Any]:
    hoje = date.today().isoformat()
    with db.conectar() as conn:
        ativos = int(conn.execute("SELECT COUNT(*) FROM ativos").fetchone()[0])
        com_dados_hoje = int(
            conn.execute(
                "SELECT COUNT(DISTINCT ticker) FROM cotacoes WHERE data = ?", (hoje,)
            ).fetchone()[0]
        )
        coleta = conn.execute(
            """
            SELECT id, iniciado_em, terminado_em, tipo, status
              FROM coletas
             ORDER BY id DESC
             LIMIT 1
            """
        ).fetchone()
        mapeados = sorted(str(ticker) for ticker in config.ler_mapa_fii())
        filtro_mapeamento = ""
        parametros: list[Any] = []
        if mapeados:
            marcadores = ", ".join("?" for _ in mapeados)
            filtro_mapeamento = (
                " AND NOT (fonte = 'cvm' AND etapa = 'mapeamento' "
                f"AND ticker IN ({marcadores}))"
            )
            parametros = mapeados
        falhas = conn.execute(
            f"""
            SELECT COUNT(*) AS registros,
                   COUNT(DISTINCT COALESCE(fonte, '') || char(31) || COALESCE(ticker, ''))
                       AS distintos
              FROM falhas
             WHERE julianday(quando) >= julianday('now', '-1 day')
             {filtro_mapeamento}
            """,
            parametros,
        ).fetchone()
        falhas_24h_registros = int(falhas["registros"])
        falhas_24h_distintos = int(falhas["distintos"])
    return {
        "ativos": ativos,
        "com_dados_hoje": com_dados_hoje,
        "ultima_coleta": _dict(coleta) if coleta is not None else None,
        "falhas_24h": falhas_24h_distintos,
        "falhas_24h_registros": falhas_24h_registros,
        "falhas_24h_distintos": falhas_24h_distintos,
        "hoje": hoje,
    }


def resumo_fontes() -> list[dict[str, Any]]:
    limitacoes = {
        "yahoo": (
            "O yfinance às vezes não publica a barra do dia; o fechamento mais "
            "recente pode faltar até a próxima coleta."
        ),
        "fundamentus": (
            "Fonte fundamentalista apenas de ação: get_resultado() está "
            "quebrado e o Fundamentus não cobre FII de papel/infra."
        ),
        "statusinvest": (
            "Fonte de ETF lida do HTML público: patrimônio líquido, cotistas, "
            "liquidez e taxa de administração. A taxa falta em parte dos "
            "fundos (o site publica \"–\"); preço e histórico continuam no yfinance."
        ),
        "fundsexplorer": (
            "Fonte de FII lida do HTML público (sem API): DY, 52 semanas, "
            "liquidez, nº de imóveis e vacância. A vacância é mensal e só "
            "existe para fundo de tijolo; Fi-Infra e FIP vêm sem valor "
            "patrimonial — nesses o VP continua vindo da CVM."
        ),
        "cvm": (
            "A CVM depende do mapa ticker–CNPJ; FII sem mapeamento fica sem VPA, "
            "cotistas e taxa de administração."
        ),
        "msn": (
            "O consenso do MSN depende da página de cada ticker; FIIs e ETFs "
            "normalmente não têm contagem de analistas nem preço-alvo."
        ),
        "genial": (
            "O preço-alvo do Genial Analisa é lido pelo leitor público "
            "r.jina.ai (o site recusa a conexão da VM); só ação e BDR têm "
            "card, e sem cobertura o alvo não é gravado."
        ),
        "btg": (
            "O preço-alvo do BTG vem da página pública de cada ativo "
            "(/research/ativo/TICKER), que é renderizada no servidor; o "
            "Stock Guide em Excel exige login de cliente e não é usado."
        ),
        "okx": (
            "Cripto é lido do par em reais da OKX (BTC-BRL, API v5 pública); "
            "a OKX devolve no máximo 100 candles por requisição, então o "
            "histórico longo é paginado."
        ),
        "sistema": "Falhas internas de coleta/ranking, sem fonte externa associada.",
    }
    with db.conectar() as conn:
        fontes = {
            linha["fonte"]
            for linha in conn.execute(
                "SELECT DISTINCT fonte FROM falhas WHERE fonte IS NOT NULL"
            ).fetchall()
        }
        fontes.update(
            {
                "yahoo",
                "fundamentus",
                "fundsexplorer",
                "statusinvest",
                "cvm",
                "msn",
                "genial",
                "btg",
                "okx",
            }
        )
        resultado = []
        for fonte in sorted(fontes):
            linha = conn.execute(
                """
                SELECT MAX(quando) AS ultima_falha, COUNT(*) AS falhas
                  FROM falhas WHERE fonte = ?
                """,
                (fonte,),
            ).fetchone()
            coletas = []
            for tabela in ("cotacoes", "fundamentos", "proventos"):
                valor = conn.execute(
                    f"SELECT MAX(coletado_em) FROM {tabela} WHERE fonte = ?", (fonte,)
                ).fetchone()[0]
                if valor:
                    coletas.append(valor)
            registros = 0
            for tabela in ("cotacoes", "fundamentos", "proventos"):
                registros += int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {tabela} WHERE fonte = ?", (fonte,)
                    ).fetchone()[0]
                )
            resultado.append(
                {
                    "fonte": fonte,
                    "ultima_coleta": max(coletas) if coletas else None,
                    "ultima_falha": linha["ultima_falha"],
                    "falhas": int(linha["falhas"]),
                    "registros": registros,
                    "limitacao": limitacoes.get(fonte, "limitação não documentada."),
                }
            )
    return resultado


def ultima_coleta() -> dict[str, Any] | None:
    with db.conectar() as conn:
        linha = conn.execute(
            "SELECT * FROM coletas ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _dict(linha) if linha is not None else None


def valor_item(item: dict[str, Any], chave: str) -> Any:
    if chave == "consenso":
        partes = []
        for sufixo in ("compra", "neutro", "venda"):
            valor = valor_item(item, f"consenso_{sufixo}")
            if valor is None:
                return None
            partes.append(f"{int(float(valor))}{sufixo[0].upper()}")
        return " · ".join(partes)
    if chave == "alvo":
        return valor_item(item, "alvo_medio")
    # Primeiro valor NÃO nulo: ``indicadores.dy`` pode existir como None
    # enquanto a estratégia já resolveu o campo em ``dados`` (DY calculado de
    # FII sem Fundamentus). Devolver o None escondia o número na tabela.
    for fonte in (
        item,
        item.get("indicadores") or {},
        item.get("dados") or {},
    ):
        if fonte.get(chave) is not None:
            return fonte.get(chave)
    return None
