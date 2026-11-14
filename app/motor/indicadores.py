"""Indicadores fundamentalistas puros (sem I/O).

Todas as funções devolvem ``None`` nos casos-limite documentados, em vez de
inventar um número.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

RAZAO_ALVO_MIN = 0.5
RAZAO_ALVO_MAX = 2.5


def _ausente(*valores: Any) -> bool:
    return any(valor is None for valor in valores)


def _numero(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    return numero


def avaliar_alvo(alvo: Any, preco: Any) -> tuple[float | None, bool]:
    """Upside e suspeita de classe errada no alvo do consenso.

    O MSN publica o alvo por empresa; a página de um ticker pode devolver o
    alvo de outra classe do papel (unit/ON/PN). Quando a razão ``alvo/preço``
    sai de ``[0,5; 2,5]`` o upside não é exibido e a flag fica ligada.
    """

    valor_alvo = _numero(alvo)
    valor_preco = _numero(preco)
    if valor_alvo is None or valor_preco is None or valor_preco <= 0:
        return None, False
    razao = valor_alvo / valor_preco
    if razao < RAZAO_ALVO_MIN or razao > RAZAO_ALVO_MAX:
        return None, True
    return (razao - 1) * 100, False


def pl(preco: float | None, lpa: float | None) -> float | None:
    """P/L = preço / LPA. ``None`` se o LPA for <= 0 (prejuízo não tem P/L)."""

    if _ausente(preco, lpa) or lpa <= 0:
        return None
    return preco / lpa


def pvp(preco: float | None, vpa: float | None) -> float | None:
    """P/VP = preço / valor patrimonial por ação. ``None`` se o VPA for <= 0."""

    if _ausente(preco, vpa) or vpa <= 0:
        return None
    return preco / vpa


def dividend_yield(proventos_por_acao: float | None, preco: float | None) -> float | None:
    """Dividend yield (%) = proventos por ação / preço * 100.

    ``None`` se o preço for <= 0.
    """

    if _ausente(proventos_por_acao, preco) or preco <= 0:
        return None
    return proventos_por_acao / preco * 100


def roe(lucro_liquido: float | None, pl_medio: float | None) -> float | None:
    """ROE (%) = lucro líquido / patrimônio líquido médio * 100.

    ``None`` se o patrimônio líquido médio for <= 0.
    """

    if _ausente(lucro_liquido, pl_medio) or pl_medio <= 0:
        return None
    return lucro_liquido / pl_medio * 100


def roic(nopat: float | None, capital_investido: float | None) -> float | None:
    """ROIC (%) = NOPAT / capital investido * 100.

    ``None`` se o capital investido for <= 0.
    """

    if _ausente(nopat, capital_investido) or capital_investido <= 0:
        return None
    return nopat / capital_investido * 100


def nopat(ebit: float | None, aliquota: float = 0.34) -> float | None:
    """NOPAT = EBIT * (1 - alíquota de imposto)."""

    if _ausente(ebit, aliquota):
        return None
    return ebit * (1 - aliquota)


def ev_ebitda(valor_firma: float | None, ebitda: float | None) -> float | None:
    """EV/EBITDA = valor da firma / EBITDA. ``None`` se o EBITDA for <= 0."""

    if _ausente(valor_firma, ebitda) or ebitda <= 0:
        return None
    return valor_firma / ebitda


def ev_ebit(valor_firma: float | None, ebit: float | None) -> float | None:
    """EV/EBIT = valor da firma / EBIT. ``None`` se o EBIT for <= 0."""

    if _ausente(valor_firma, ebit) or ebit <= 0:
        return None
    return valor_firma / ebit


def divida_liquida_ebitda(
    divida_liquida: float | None, ebitda: float | None
) -> float | None:
    """Dívida líquida / EBITDA. ``None`` se o EBITDA for <= 0."""

    if _ausente(divida_liquida, ebitda) or ebitda <= 0:
        return None
    return divida_liquida / ebitda


def margem(resultado: float | None, receita: float | None) -> float | None:
    """Margem (%) = resultado / receita * 100. ``None`` se a receita for <= 0."""

    if _ausente(resultado, receita) or receita <= 0:
        return None
    return resultado / receita * 100


def cagr(inicial: float | None, final: float | None, anos: float | None) -> float | None:
    """CAGR (%) = ((final / inicial) ** (1 / anos) - 1) * 100.

    ``None`` se o valor inicial for <= 0, os anos forem <= 0 ou o valor final
    for negativo.
    """

    if _ausente(inicial, final, anos) or inicial <= 0 or anos <= 0 or final < 0:
        return None
    return ((final / inicial) ** (1 / anos) - 1) * 100


def payout(dividendos: float | None, lucro: float | None) -> float | None:
    """Payout (%) = dividendos / lucro líquido * 100.

    ``None`` se o lucro for <= 0.
    """

    if _ausente(dividendos, lucro) or lucro <= 0:
        return None
    return dividendos / lucro * 100


def valor_firma(
    preco: float | None,
    nro_acoes: float | None,
    divida_bruta: float | None,
    caixa: float | None,
) -> float | None:
    """Valor da firma = preço * nº ações + dívida bruta - caixa."""

    if _ausente(preco, nro_acoes, divida_bruta, caixa):
        return None
    return preco * nro_acoes + divida_bruta - caixa


def retorno_total_12m(
    cotacoes: Any,
    proventos: Any,
    referencia: date | None = None,
) -> dict[str, Any]:
    """Retorno total (LTM) = valorização da cota + proventos no período.

    O corte é ``referencia - 365 dias``. O preço inicial é a última cotação
    com data menor ou igual ao corte (nunca a cotação mais antiga disponível):
    sem essa cotação o retorno fica ``None``. Não arredonda e ignora linhas
    inválidas (data inválida, fechamento nulo/não positivo, valor nulo).
    """

    referencia = referencia or date.today()
    limite = referencia - timedelta(days=365)

    melhor_final: tuple[date, float] | None = None
    melhor_inicial: tuple[date, float] | None = None
    for linha in cotacoes or []:
        try:
            quando = date.fromisoformat(str(linha.get("data"))[:10])
        except (TypeError, ValueError):
            continue
        preco = _numero(linha.get("fechamento"))
        if preco is None or preco <= 0:
            continue
        if quando <= referencia and (
            melhor_final is None or quando > melhor_final[0]
        ):
            melhor_final = (quando, preco)
        if quando <= limite and (
            melhor_inicial is None or quando > melhor_inicial[0]
        ):
            melhor_inicial = (quando, preco)

    proventos_12m = 0.0
    for item in proventos or []:
        try:
            quando = date.fromisoformat(str(item.get("data_ex"))[:10])
        except (TypeError, ValueError):
            continue
        valor = _numero(item.get("valor"))
        if valor is None:
            continue
        if quando >= limite:
            proventos_12m += valor

    resultado: dict[str, Any] = {
        "preco_inicial": None,
        "data_inicial": None,
        "data_final": None,
        "preco_final": None,
        "proventos_12m": proventos_12m,
        "valorizacao_12m_pct": None,
        "proventos_12m_pct": None,
        "retorno_total_12m_pct": None,
    }
    if melhor_final is not None:
        resultado["data_final"] = melhor_final[0].isoformat()
        resultado["preco_final"] = melhor_final[1]
    if melhor_inicial is None:
        return resultado

    resultado["data_inicial"] = melhor_inicial[0].isoformat()
    resultado["preco_inicial"] = melhor_inicial[1]
    resultado["proventos_12m_pct"] = proventos_12m / melhor_inicial[1] * 100
    if melhor_final is None:
        return resultado

    resultado["valorizacao_12m_pct"] = (
        melhor_final[1] / melhor_inicial[1] - 1
    ) * 100
    resultado["retorno_total_12m_pct"] = (
        resultado["valorizacao_12m_pct"] + resultado["proventos_12m_pct"]
    )
    return resultado


MESES_CURTOS = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)


def regularidade_proventos_24m(
    proventos: Any,
    referencia: date | None = None,
) -> dict[str, Any]:
    """Cobertura de proventos nos últimos 24 meses (mês a mês).

    A janela são os 24 meses fechados até o mês da referência: o denominador é
    sempre 24, então um ativo que paga todo mês marca 100% e um que paga uma
    vez por ano marca ~4%. ``meses_tipicos`` são os meses que tiveram pagamento
    nos DOIS anos da janela (o que separa cadência anual de pagamento avulso).
    """

    referencia = referencia or date.today()
    indice_inicial = (referencia.year * 12 + referencia.month - 1) - 23
    ano_inicial, mes_inicial = divmod(indice_inicial, 12)
    inicio = date(ano_inicial, mes_inicial + 1, 1)
    meio = date(ano_inicial + 1, mes_inicial + 1, 1)

    por_mes: dict[tuple[int, int], int] = {}
    for item in proventos or []:
        try:
            quando = date.fromisoformat(str(item.get("data_ex"))[:10])
        except (TypeError, ValueError):
            continue
        if quando < inicio or quando > referencia:
            continue
        if _numero(item.get("valor")) is None:
            continue
        chave = (quando.year, quando.month)
        por_mes[chave] = por_mes.get(chave, 0) + 1

    primeiro_ano = {mes for (ano, mes) in por_mes if date(ano, mes, 1) < meio}
    segundo_ano = {mes for (ano, mes) in por_mes if date(ano, mes, 1) >= meio}
    tipicos = sorted(primeiro_ano & segundo_ano)
    meses = len(por_mes)
    pagamentos = sum(por_mes.values())
    nomes = [MESES_CURTOS[mes - 1] for mes in tipicos]

    if pagamentos == 0:
        observacao = "sem provento nos últimos 24 meses"
    elif len(tipicos) == 12:
        observacao = "paga todos os meses"
    elif tipicos:
        observacao = "paga geralmente em " + ", ".join(nomes)
    else:
        avulsos = sorted({mes for (_, mes) in por_mes})
        observacao = (
            "sem recorrência anual; pagou em "
            + ", ".join(MESES_CURTOS[mes - 1] for mes in avulsos)
        )

    return {
        "regularidade_24m_meses": meses,
        "regularidade_24m_pct": round(meses / 24 * 100, 1),
        "proventos_24m_qtd": pagamentos,
        "proventos_24m_tipicos": nomes,
        "proventos_24m_inicio": inicio.isoformat(),
        "proventos_24m_observacao": observacao,
    }


INDICADORES: dict[str, dict[str, str]] = {
    "pl": {
        "formula": "Preço da ação dividido pelo lucro por ação (LPA).",
        "unidade": "x",
        "na_quando": "Quando o LPA é menor ou igual a zero (prejuízo).",
    },
    "pvp": {
        "formula": "Preço da ação dividido pelo valor patrimonial por ação (VPA).",
        "unidade": "x",
        "na_quando": "Quando o VPA é menor ou igual a zero (patrimônio negativo).",
    },
    "dividend_yield": {
        "formula": "Proventos por ação divididos pelo preço da ação, em percentual.",
        "unidade": "%",
        "na_quando": "Quando o preço é menor ou igual a zero.",
    },
    "roe": {
        "formula": "Lucro líquido dividido pelo patrimônio líquido médio, em percentual.",
        "unidade": "%",
        "na_quando": "Quando o patrimônio líquido médio é menor ou igual a zero.",
    },
    "roic": {
        "formula": "NOPAT dividido pelo capital investido, em percentual.",
        "unidade": "%",
        "na_quando": "Quando o capital investido é menor ou igual a zero.",
    },
    "nopat": {
        "formula": "EBIT multiplicado por (1 - alíquota de imposto).",
        "unidade": "R$",
        "na_quando": "Quando o EBIT ou a alíquota não estão disponíveis.",
    },
    "ev_ebitda": {
        "formula": "Valor da firma dividido pelo EBITDA.",
        "unidade": "x",
        "na_quando": "Quando o EBITDA é menor ou igual a zero.",
    },
    "ev_ebit": {
        "formula": "Valor da firma dividido pelo EBIT.",
        "unidade": "x",
        "na_quando": "Quando o EBIT é menor ou igual a zero.",
    },
    "divida_liquida_ebitda": {
        "formula": "Dívida líquida dividida pelo EBITDA.",
        "unidade": "x",
        "na_quando": "Quando o EBITDA é menor ou igual a zero.",
    },
    "margem": {
        "formula": "Resultado dividido pela receita líquida, em percentual.",
        "unidade": "%",
        "na_quando": "Quando a receita líquida é menor ou igual a zero.",
    },
    "cagr": {
        "formula": "((valor final / valor inicial) ^ (1 / anos) - 1) em percentual.",
        "unidade": "%",
        "na_quando": "Quando o valor inicial ou os anos são menores ou iguais a zero, "
        "ou o valor final é negativo. O CAGR de receita também fica indisponível "
        "enquanto não houver série plurianual de receita na fonte; Cres_Rec_5a é "
        "exibido separadamente como crescimento_receita_5a e não é CAGR.",
    },
    "payout": {
        "formula": "Soma dos proventos por ação dos últimos 12 meses dividida "
        "pelo lucro por ação, em percentual.",
        "unidade": "%",
        "na_quando": "Quando o lucro por ação é menor ou igual a zero ou não há "
        "provento no período.",
    },
    "valor_firma": {
        "formula": "Preço da ação * número de ações + dívida bruta - caixa.",
        "unidade": "R$",
        "na_quando": "Quando preço, número de ações, dívida bruta ou caixa faltam.",
    },
}


def _proventos_12m(ticker: str | None) -> float | None:
    """Soma dos proventos **por ação** da fonte yahoo nos últimos 365 dias."""

    if not ticker:
        return None
    try:
        from app import db
    except Exception:  # pragma: no cover - import defensivo
        return None

    try:
        linhas = db.consultar(
            """
            SELECT COALESCE(SUM(valor), 0) AS total
              FROM proventos
             WHERE ticker = ?
               AND fonte = 'yahoo'
               AND data_ex >= date('now', '-365 day')
            """,
            (ticker,),
        )
    except Exception:  # pragma: no cover - banco indisponível não derruba a tela
        return None

    if not linhas:
        return None
    total = linhas[0]["total"]
    if total is None or total <= 0:
        return None
    return float(total)


def avaliar_fundamentalista(
    campos: dict[str, Any],
    ticker: str | None = None,
    proventos_12m: float | None = None,
    preco_fechamento: float | None = None,
    classe: str | None = None,
    consultar_proventos: bool = True,
) -> dict[str, Any]:
    """Monta o pacote de indicadores a partir dos campos canônicos.

    Usa o valor informado pela fonte quando existir; caso contrário, calcula a
    partir dos campos disponíveis. Onde não há dado suficiente, devolve ``None``
    (não estima).

    Derivados:

    - ``valor_firma = preco * nro_acoes + divida_bruta - caixa`` (``None`` se
      faltar qualquer termo); ``preco`` é o fechamento de mercado quando
      informado (o preço de mercado manda quando existe);
    - ``ebitda = valor_firma / ev_ebitda`` **somente** quando ``ev_ebitda > 0``;
      nesse caso ``ebitda_derivado`` é ``True`` para a tela avisar que o valor
      vem de uma razão;
    - ``ev_ebitda_calculado = valor_firma / ebitda`` e
      ``divida_liquida_ebitda = divida_liquida / ebitda``;
    - ``payout = proventos por ação dos últimos 12 meses / LPA * 100``,
      usando a tabela ``proventos`` (valores já por ação, fonte yahoo);
      ``None`` se não houver proventos ou LPA <= 0;
    - ``dy_12m_calculado = proventos por ação dos últimos 12 meses /
      último fechamento * 100``; ``None`` se faltar preço ou provento;
    - ``pvp``: cadeia explícita com a origem em ``pvp_origem``:
      ``pvp`` do Fundamentus (``"fundamentus"``) → ``pvp_yahoo``
      (``priceToBook`` do yfinance, ``"yahoo"``) → ``preço / vpa_cvm``
      (``"cvm"``) → ``preço / vpa_calculado`` (``"calculado"``).
      Para **FII** a CVM vem antes do yfinance, porque o VPA da CVM é a
      referência do setor. Para **BDR** o ``priceToBook`` do papel-fonte em
      dólar é uma razão, então vale como P/VP: o preço do BDR não entra na
      conta, só a razão.
    """

    def obter(chave: str) -> Any:
        return campos.get(chave)

    preco = obter("preco")
    # preço de mercado manda quando existe
    preco_mercado = preco_fechamento if preco_fechamento is not None else preco
    lpa = obter("lpa")
    vpa = obter("vpa_calculado")
    lucro = obter("lucro_12m")
    patrimonio = obter("patrimonio_liquido")
    ebit = obter("ebit_12m")
    receita = obter("receita_12m")
    divida_bruta = obter("divida_bruta")
    caixa = obter("caixa")
    nro_acoes = obter("nro_acoes")

    vpa_cvm = obter("vpa_cvm")

    calculado_pl = pl(preco_mercado, lpa)
    calculado_pvp = pvp(preco_mercado, vpa)
    calculado_roe = roe(lucro, patrimonio)

    pvp_fonte = obter("pvp")
    pvp_yahoo = obter("pvp_yahoo")
    pvp_valor = None
    pvp_origem = None
    if pvp_fonte is not None:
        pvp_valor = pvp_fonte
        pvp_origem = "fundamentus"
    elif classe == "fii" and vpa_cvm is not None:
        pvp_valor = pvp(preco_mercado, vpa_cvm)
        if pvp_valor is not None:
            pvp_origem = "cvm"
    if pvp_valor is None and pvp_yahoo is not None:
        pvp_valor = pvp_yahoo
        pvp_origem = "yahoo"
    if pvp_valor is None and classe != "fii" and vpa_cvm is not None:
        pvp_valor = pvp(preco_mercado, vpa_cvm)
        if pvp_valor is not None:
            pvp_origem = "cvm"
    if pvp_valor is None and calculado_pvp is not None:
        pvp_valor = calculado_pvp
        pvp_origem = "calculado"

    valor = valor_firma(preco_mercado, nro_acoes, divida_bruta, caixa)
    ev_ebitda_fonte = obter("ev_ebitda")
    ebitda = obter("ebitda")
    ebitda_derivado = False
    if (
        ebitda is None
        and valor is not None
        and ev_ebitda_fonte is not None
        and ev_ebitda_fonte > 0
    ):
        ebitda = valor / ev_ebitda_fonte
        ebitda_derivado = True

    divida_liquida = obter("divida_liquida")
    if divida_liquida is None and divida_bruta is not None and caixa is not None:
        divida_liquida = divida_bruta - caixa

    if proventos_12m is None and consultar_proventos:
        proventos_12m = _proventos_12m(ticker)

    calculado_payout = None
    if (
        proventos_12m is not None
        and proventos_12m > 0
        and lpa is not None
        and lpa > 0
    ):
        calculado_payout = proventos_12m / lpa * 100

    dy_12m_calculado = dividend_yield(proventos_12m, preco_mercado)
    upside_alvo, alvo_duvidoso = avaliar_alvo(obter("alvo_medio"), preco_mercado)

    return {
        "pl": obter("pl") if obter("pl") is not None else calculado_pl,
        "pvp": pvp_valor,
        "pvp_origem": pvp_origem,
        "dy": obter("dy"),
        "roe": obter("roe") if obter("roe") is not None else calculado_roe,
        "roic": obter("roic"),
        "ev_ebitda": ev_ebitda_fonte,
        "ev_ebitda_calculado": ev_ebitda(valor, ebitda),
        "ev_ebit": obter("ev_ebit"),
        "ebitda": ebitda,
        "ebitda_derivado": ebitda_derivado,
        "divida_liquida": divida_liquida,
        "divida_liquida_ebitda": divida_liquida_ebitda(divida_liquida, ebitda),
        "margem_bruta": obter("margem_bruta"),
        "margem_ebit": obter("margem_ebit")
        if obter("margem_ebit") is not None
        else margem(ebit, receita),
        "margem_liquida": obter("margem_liquida")
        if obter("margem_liquida") is not None
        else margem(lucro, receita),
        "lpa": lpa,
        # FII não tem vpa_calculado (Fundamentus não entrega o conjunto de
        # contas); o valor patrimonial da cota vem da CVM.
        "vpa": vpa if vpa is not None else (vpa_cvm if classe == "fii" else None),
        "payout": calculado_payout,
        "dy_12m_calculado": dy_12m_calculado,
        "cagr": cagr(obter("valor_inicial"), obter("valor_final"), obter("anos")),
        "cagr_receita": None,
        "crescimento_receita_5a": obter("crescimento_receita_5a"),
        "valor_firma": valor,
        "ebit": ebit,
        "ebit_12m": ebit,
        "upside_pct": upside_alvo,
        "alvo_classe_duvidosa": alvo_duvidoso,
    }
