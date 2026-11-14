"""Estratégias de seleção puras.

Cada estratégia recebe um dicionário de campos já normalizados (preço,
indicadores fundamentalistas, setor, histórico de proventos) e uma configuração
versionada. Sem dado essencial a estratégia não pontua: devolve
``elegivel=False`` e o motivo em ``indisponiveis``.
"""

from __future__ import annotations

import math
import statistics
import unicodedata
from datetime import date, timedelta
from typing import Any, Callable

SETORES_FINANCEIROS_PADRAO = [
    "intermediarios financeiros",
    "previdencia e seguros",
    "bancos",
    "seguradoras",
    "servicos financeiros",
    "holdings financeiras",
]

CONFIG_PADRAO: dict[str, Any] = {
    "versao": 1,
    "setores_financeiros": list(SETORES_FINANCEIROS_PADRAO),
    "magic_formula": {},
    "earnings_yield": {"versao": 1},
    "upside_consenso": {"versao": 1, "min_analistas": 3},
    "pl_roe": {"versao": 1},
    "graham": {
        "pl_max": 15.0,
        "pvp_max": 1.5,
        "pl_pvp_max": 22.5,
        "div_patrim_max": 1.0,
        "liq_corr_min": 1.0,
    },
    "bazin": {
        "taxa_exigida": 0.06,
        "periodo_dividendos_anos": 5,
        "min_anos_dividendos": 3,
        "dy_alerta": 12.0,
        "multiplo_provento_alerta": 2.0,
    },
    "dividendos": {
        "pesos": {
            "dy": 0.25,
            "regularidade": 0.20,
            "crescimento": 0.15,
            "payout": 0.15,
            "divida_liquida_ebitda": 0.15,
            "roe": 0.10,
        },
        "dy_bom": 6.0,
        "regularidade_min_meses": 18,
        "payout_max": 80.0,
        "divida_max": 3.0,
        "roe_bom": 12.0,
    },
    "qualidade_crescimento": {
        "pesos": {
            "roic": 0.25,
            "roe": 0.20,
            "margem_liquida": 0.15,
            "crescimento_receita_5a": 0.15,
            "conversao_caixa": 0.10,
            "divida_liquida_ebitda": 0.10,
            "estabilidade_roe": 0.05,
        },
        "divida_max": 3.0,
        "estabilidade_max": 5.0,
    },
    # liquidez_min é valor em R$ por dia, mesma unidade do Vol_med_2m do
    # Fundamentus. Não confundir com quantidade de ações/quotas negociadas.
    "acoes": {
        "liquidez_min": 200_000.0,
    },
    "bdr": {
        "liquidez_min": 100_000.0,
    },
    "fiis": {
        "pesos": {
            "dy": 40,
            "pvp": 30,
            "liquidez": 20,
            "regularidade": 10,
        },
        "dy_min": 8.0,
        "dy_bom": 8.0,
        "liquidez_min": 5_000.0,
    },
    "etfs": {
        "pesos": {"liquidez": 0.35, "retorno": 0.35, "volatilidade": 0.30},
        "liquidez_min": 50_000.0,
        "volatilidade_max": 35.0,
        "volatilidade_min": 5.0,
    },
    "screening": {
        "tipo": "ema",
        "janela_cruzamento_dias": 10,
        "tolerancia_pullback_pct": 1.5,
        "janela_pullback_dias": 3,
        "limiar_preco_m9_pct": 8.0,
        "limiar_m9_m21_pct": 4.0,
        "historico_minimo": 60,
    },
}


def _num(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if math.isnan(numero) or math.isinf(numero):
        return None
    return numero


def _valor(ativo: dict[str, Any], *chaves: str) -> float | None:
    for chave in chaves:
        numero = _num(ativo.get(chave))
        if numero is not None:
            return numero
    return None


def _data_ref(ativo: dict[str, Any]) -> str:
    return str(ativo.get("data_ref") or ativo.get("data_cotacao") or "")


def _com_casas(valor: Any, sufixo: str = "", casas: int = 2) -> str:
    """Número em texto com casas fixas; ``None`` vira ``sem dado``.

    Motivos são texto exibido na tela: sem casas fixas o float cru vaza
    (``DY 12m: 10.573690594084187%``).
    """

    if valor is None:
        return "sem dado"
    return f"{valor:.{casas}f}{sufixo}"


def _resultado(
    *,
    elegivel: bool,
    pontuacao: float | None,
    aprovados: list[str],
    reprovados: list[str],
    indisponiveis: list[str],
    motivos: dict[str, str],
    ativo: dict[str, Any],
    dados: dict[str, Any] | None = None,
    avisos: list[str] | None = None,
    destaque: str | None = None,
) -> dict[str, Any]:
    return {
        "elegivel": bool(elegivel),
        "pontuacao": None if pontuacao is None else float(pontuacao),
        "aprovados": aprovados,
        "reprovados": reprovados,
        "indisponiveis": indisponiveis,
        "motivos": motivos,
        "data_ref": _data_ref(ativo),
        "dados": dados or {},
        "avisos": avisos or [],
        "destaque": destaque,
    }


def _normalizar_texto(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    return "".join(caractere for caractere in texto if not unicodedata.combining(caractere)).lower()


def _setor_financeiro(ativo: dict[str, Any], config: dict[str, Any]) -> bool:
    setor = _normalizar_texto(ativo.get("setor"))
    subsetor = _normalizar_texto(ativo.get("subsetor"))
    if not setor and not subsetor:
        return False
    termos = config.get("setores_financeiros") or SETORES_FINANCEIROS_PADRAO
    for termo in termos:
        alvo = _normalizar_texto(termo)
        if alvo and (alvo in setor or alvo in subsetor):
            return True
    return False


def _soma(partes: dict[str, float | None]) -> float | None:
    if any(valor is None for valor in partes.values()):
        return None
    return float(sum(valor for valor in partes.values() if valor is not None))


def _media(valores: list[float]) -> float | None:
    validos = [valor for valor in valores if valor is not None]
    if not validos:
        return None
    return float(sum(validos) / len(validos))


def _normalizar(valor: float | None, pior: float, melhor: float) -> float | None:
    if valor is None or pior == melhor:
        return None
    proporcao = (valor - pior) / (melhor - pior)
    return max(0.0, min(1.0, proporcao)) * 100


def _normalizar_inverso(valor: float | None, bom: float, ruim: float) -> float | None:
    if valor is None or bom == ruim:
        return None
    proporcao = (ruim - valor) / (ruim - bom)
    return max(0.0, min(1.0, proporcao)) * 100


def _brl_diario(valor: float) -> str:
    """Formata R$ por dia no padrão brasileiro, sem centavos."""

    return f"R$ {valor:,.0f}".replace(",", ".")


def _texto_liquidez(valor: float, minimo: float) -> str:
    texto = f"liquidez {_brl_diario(valor)}/dia"
    if valor < minimo:
        return f"{texto} abaixo do mínimo {_brl_diario(minimo)}/dia"
    return f"{texto} (mínimo {_brl_diario(minimo)}/dia)"


def _media_ponderada(
    notas: dict[str, float | None], pesos: dict[str, float]
) -> tuple[float | None, float]:
    disponiveis = {chave: nota for chave, nota in notas.items() if nota is not None}
    if not disponiveis:
        return None, 0.0
    soma_pesos = sum(pesos.get(chave, 0.0) for chave in disponiveis)
    if soma_pesos <= 0:
        return None, 0.0
    total = sum(nota * pesos.get(chave, 0.0) for chave, nota in disponiveis.items())
    return float(total / soma_pesos), soma_pesos


def magic_formula(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Magic Formula: earnings yield (EBIT/EV) e retorno sobre o capital.

    Bancos e seguradoras ficam fora porque EV/EBIT não tem sentido para
    financeiras. A elegibilidade exige EBIT > 0, EV > 0 e capital investido > 0.
    A pontuação não é calculada aqui: ela é a soma dos postos de earnings yield
    e retorno sobre o capital, atribuída no corte transversal do ranking.
    """

    if _setor_financeiro(ativo, config):
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=[],
            reprovados=["setor_financeiro"],
            indisponiveis=[],
            motivos={
                "setor_financeiro": "setor financeiro fica fora da Magic Formula: "
                "EV/EBIT não é aplicável a bancos e seguradoras"
            },
            ativo=ativo,
        )

    ebit = _valor(ativo, "ebit_12m", "ebit")
    ev = _valor(ativo, "valor_firma", "ev", "enterprise_value")
    capital = _valor(ativo, "capital_investido")
    if capital is None:
        patrimonio = _valor(ativo, "patrimonio_liquido")
        divida = _valor(ativo, "divida_liquida")
        if patrimonio is not None and divida is not None:
            capital = patrimonio + divida

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    for nome, valor, texto in (
        ("ebit_positivo", ebit, "EBIT dos últimos 12 meses"),
        ("valor_firma", ev, "valor da firma (EV)"),
        ("capital_investido", capital, "capital investido (patrimônio + dívida líquida)"),
    ):
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = f"sem {texto}"

    if indisponiveis:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
        )

    assert ebit is not None and ev is not None and capital is not None
    if ebit <= 0:
        reprovados.append("ebit_positivo")
        motivos["ebit_positivo"] = "EBIT não é positivo; a fórmula exige lucro operacional"
    else:
        aprovados.append("ebit_positivo")
        motivos["ebit_positivo"] = "EBIT positivo"

    if ev <= 0:
        reprovados.append("valor_firma")
        motivos["valor_firma"] = "EV não é positivo"
    else:
        aprovados.append("valor_firma")
        motivos["valor_firma"] = "EV positivo"

    if capital <= 0:
        reprovados.append("capital_investido")
        motivos["capital_investido"] = "capital investido não é positivo"
    else:
        aprovados.append("capital_investido")
        motivos["capital_investido"] = "capital investido positivo"

    if reprovados:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
        )

    earnings_yield = ebit / ev
    return_on_capital = ebit / capital
    ev_ebit = 1 / earnings_yield if earnings_yield > 0 else None
    motivos["earnings_yield"] = f"EBIT/EV = {earnings_yield:.4f}"
    motivos["return_on_capital"] = f"EBIT/capital investido = {return_on_capital:.4f}"

    return _resultado(
        elegivel=True,
        pontuacao=None,
        aprovados=["earnings_yield", "return_on_capital"],
        reprovados=[],
        indisponiveis=[],
        motivos=motivos,
        ativo=ativo,
        dados={
            "earnings_yield": float(earnings_yield),
            "return_on_capital": float(return_on_capital),
            "ev_ebit": float(ev_ebit) if ev_ebit is not None else None,
            "ebit_ev_pct": float(earnings_yield) * 100,
            "ebit": ebit,
            "valor_firma": ev,
            "capital_investido": capital,
        },
    )


def earnings_yield(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Earnings yield puro: EBIT/EV, em percentual, maior melhor.

    Bancos e seguradoras ficam fora: as aplicações financeiras entram como
    caixa no cálculo do EV, o denominador despenca e o earnings yield explode.
    Para financeiras a lista correta é a ``pl_roe``. Sem EBIT ou sem EV
    positivo o ativo fica inelegível.
    """

    if _setor_financeiro(ativo, config):
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=[],
            reprovados=["ev_nao_representativo"],
            indisponiveis=[],
            motivos={
                "ev_nao_representativo": (
                    "EV não é representativo para instituição financeira "
                    "(aplicações financeiras entram como caixa); use a lista P/L × ROE"
                )
            },
            ativo=ativo,
            dados={"setor": ativo.get("setor")},
        )

    ebit = _valor(ativo, "ebit_12m", "ebit")
    ev = _valor(ativo, "valor_firma", "ev", "enterprise_value")

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}
    avisos: list[str] = []

    if ebit is None:
        indisponiveis.append("ebit_positivo")
        motivos["ebit_positivo"] = "sem EBIT dos últimos 12 meses"
    elif ebit <= 0:
        reprovados.append("ebit_positivo")
        motivos["ebit_positivo"] = "EBIT não é positivo; sem earnings yield"
    else:
        aprovados.append("ebit_positivo")
        motivos["ebit_positivo"] = "EBIT positivo"

    if ev is None:
        indisponiveis.append("valor_firma")
        motivos["valor_firma"] = "sem valor da firma (EV)"
    elif ev <= 0:
        reprovados.append("valor_firma")
        motivos["valor_firma"] = "EV não é positivo; sem earnings yield"
    else:
        aprovados.append("valor_firma")
        motivos["valor_firma"] = "EV positivo"

    if indisponiveis or reprovados:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
        )

    assert ebit is not None and ev is not None
    valor = ebit / ev

    motivos["earnings_yield"] = f"EBIT/EV = {valor:.4f}"

    return _resultado(
        elegivel=True,
        pontuacao=valor * 100,
        aprovados=aprovados + ["earnings_yield"],
        reprovados=[],
        indisponiveis=[],
        motivos=motivos,
        ativo=ativo,
        dados={
            "earnings_yield": float(valor),
            "ebit": ebit,
            "valor_firma": ev,
        },
        avisos=avisos,
    )


def upside_consenso(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Maior upside até o preço-alvo médio do consenso MSN.

    A pontuação é o próprio ``upside_pct`` calculado pelo motor de
    indicadores; esta função não recalcula o upside, para não haver duas
    verdades. Só entram ações com cobertura mínima de analistas e sem a marca
    de alvo provavelmente de outra classe do papel.
    """

    cfg = config.get("upside_consenso")
    if not isinstance(cfg, dict):
        cfg = config
    minimo = _num(cfg.get("min_analistas"))
    if minimo is None or minimo <= 0:
        minimo = 3
    minimo = int(minimo)

    preco = _valor(ativo, "preco")
    alvo_medio = _valor(ativo, "alvo_medio")
    alvo_mediana = _valor(ativo, "alvo_mediana")
    upside = _valor(ativo, "upside_pct")
    total = _valor(ativo, "consenso_total")
    compra = _valor(ativo, "consenso_compra")
    neutro = _valor(ativo, "consenso_neutro")
    venda = _valor(ativo, "consenso_venda")
    duvidosa = ativo.get("alvo_classe_duvidosa") is True
    data_consenso = (
        ativo.get("data_consenso")
        or ativo.get("consenso_data_ref")
        or _data_ref(ativo)
        or None
    )

    dados = {
        "preco": preco,
        "alvo_medio": alvo_medio,
        "alvo_mediana": alvo_mediana,
        "upside_pct": upside,
        "consenso_compra": compra,
        "consenso_neutro": neutro,
        "consenso_venda": venda,
        "consenso_total": total,
        "data_consenso": data_consenso,
    }

    def fora(chave: str, motivo: str) -> dict[str, Any]:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=[],
            reprovados=[chave],
            indisponiveis=[],
            motivos={chave: motivo},
            ativo=ativo,
            dados=dados,
        )

    if (
        preco is None
        or preco <= 0
        or alvo_medio is None
        or alvo_medio <= 0
        or total is None
        or total <= 0
    ):
        return fora("consenso", "sem consenso na fonte")

    if total < minimo:
        return fora(
            "cobertura",
            f"cobertura de {int(total)} analista(s): mínimo {minimo}",
        )

    if duvidosa:
        return fora(
            "alvo_classe_duvidosa",
            "alvo provavelmente de outra classe do papel (unit × PN/ON)",
        )

    if upside is None or upside < -50.0 or upside > 250.0:
        return fora("upside", "upside fora da faixa aceita")

    return _resultado(
        elegivel=True,
        pontuacao=upside,
        aprovados=["upside_consenso"],
        reprovados=[],
        indisponiveis=[],
        motivos={
            "upside_consenso": (
                f"upside de {upside:.1f}% até o alvo médio, "
                f"com {int(total)} analista(s)"
            )
        },
        ativo=ativo,
        dados=dados,
    )


def pl_roe(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """P/L × ROE: lista de valor para empresas, financeiras incluídas.

    É a lista que captura bancos e seguradoras, para os quais o EV não é uma
    medida válida. A elegibilidade exige P/L positivo e ROE positivo; não há
    filtro de setor nem uso de EV/EBITDA/EBIT.

    A pontuação não é ``P/L × ROE``: o produto mistura unidades (R$/R$ com %)
    e não ordena o mérito. A pontuação é a soma de postos no corte transversal,
    o mesmo desenho da Magic Formula: ``posto_pl`` (menor P/L = posto 1) mais
    ``posto_roe`` (maior ROE = posto 1). O produto ``pl_x_roe`` aparece apenas
    em ``dados``, como a métrica de bolso que o usuário conhece.
    """

    pl = _valor(ativo, "pl")
    roe = _valor(ativo, "roe")
    lpa = _valor(ativo, "lpa")
    vpa = _valor(ativo, "vpa", "vpa_calculado")
    setor = ativo.get("setor") or ativo.get("subsetor")

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    for nome, valor, rotulo in (
        ("pl", pl, "P/L"),
        ("roe", roe, "ROE"),
    ):
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = f"sem {rotulo}"

    if indisponiveis:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
            dados={"pl": pl, "roe": roe, "lpa": lpa, "vpa": vpa, "setor": setor},
        )

    assert pl is not None and roe is not None
    if pl <= 0:
        reprovados.append("pl")
        motivos["pl"] = "P/L não é positivo; a lista exige lucro"
    else:
        aprovados.append("pl")
        motivos["pl"] = f"P/L = {pl:.2f}"

    if roe <= 0:
        reprovados.append("roe")
        motivos["roe"] = "ROE não é positivo; a lista exige rentabilidade"
    else:
        aprovados.append("roe")
        motivos["roe"] = f"ROE = {roe:.2f}%"

    if _setor_financeiro(ativo, config):
        motivos["setor"] = "instituição financeira: avaliação por P/L e ROE"

    if reprovados:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
            dados={
                "pl": pl,
                "roe": roe,
                "lpa": lpa,
                "vpa": vpa,
                "setor": setor,
                "pl_x_roe": pl * roe / 100,
            },
        )

    return _resultado(
        elegivel=True,
        pontuacao=None,
        aprovados=aprovados,
        reprovados=[],
        indisponiveis=[],
        motivos=motivos,
        ativo=ativo,
        dados={
            "pl": pl,
            "roe": roe,
            "lpa": lpa,
            "vpa": vpa,
            "setor": setor,
            "pl_x_roe": pl * roe / 100,
        },
    )


def graham(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Filtro de Graham, com valor intrínseco apenas como estimativa separada."""

    cfg = config.get("graham", {})
    pl_max = _num(cfg.get("pl_max")) or 15.0
    pvp_max = _num(cfg.get("pvp_max")) or 1.5
    pl_pvp_max = _num(cfg.get("pl_pvp_max")) or 22.5
    div_patrim_max = _num(cfg.get("div_patrim_max"))
    if div_patrim_max is None:
        div_patrim_max = 1.0
    liq_corr_min = _num(cfg.get("liq_corr_min"))
    if liq_corr_min is None:
        liq_corr_min = 1.0

    pl_valor = _valor(ativo, "pl")
    pvp_valor = _valor(ativo, "pvp")
    div_patrim = _valor(ativo, "divida_liquida_patrim", "div_liquida_patrim")
    if div_patrim is None:
        divida = _valor(ativo, "divida_liquida")
        patrimonio = _valor(ativo, "patrimonio_liquido")
        if divida is not None and patrimonio is not None and patrimonio > 0:
            div_patrim = divida / patrimonio
    lucro = _valor(ativo, "lucro_12m", "lucro_liquido", "lpa")
    liquidez = _valor(ativo, "liquidez_corrente")

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    criterios = (
        ("pl", pl_valor, "P/L", f"P/L <= {pl_max:g}"),
        ("pvp", pvp_valor, "P/VP", f"P/VP <= {pvp_max:g}"),
        (
            "pl_pvp",
            None if pl_valor is None or pvp_valor is None else pl_valor * pvp_valor,
            "P/L × P/VP",
            f"P/L × P/VP <= {pl_pvp_max:g}",
        ),
        (
            "divida_liquida_patrim",
            div_patrim,
            "dívida líquida/patrimônio",
            f"dívida líquida/patrimônio <= {div_patrim_max:g}",
        ),
        ("lucro_positivo", lucro, "lucro", "lucro positivo nos dados disponíveis"),
        (
            "liquidez_corrente",
            liquidez,
            "liquidez corrente",
            f"liquidez corrente >= {liq_corr_min:g}",
        ),
    )

    for nome, valor, rotulo, regra in criterios:
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = f"sem {rotulo}"
            continue
        if nome == "pl":
            aprovado = valor <= pl_max
        elif nome == "pvp":
            aprovado = valor <= pvp_max
        elif nome == "pl_pvp":
            aprovado = valor <= pl_pvp_max
        elif nome == "divida_liquida_patrim":
            aprovado = valor <= div_patrim_max
        elif nome == "lucro_positivo":
            aprovado = valor > 0
        else:
            aprovado = valor >= liq_corr_min
        motivos[nome] = f"{rotulo} = {valor:g}; {regra}"
        (aprovados if aprovado else reprovados).append(nome)

    lpa = _valor(ativo, "lpa")
    vpa = _valor(ativo, "vpa", "vpa_calculado")
    valor_intrinseco = None
    if lpa is not None and vpa is not None and lpa > 0 and vpa > 0:
        valor_intrinseco = math.sqrt(22.5 * lpa * vpa)
        motivos["valor_intrinseco_estimado"] = (
            "estimativa de Graham: sqrt(22.5 × LPA × VPA); não é valor objetivo"
        )

    preco = _valor(ativo, "preco")
    pl_pvp = None if pl_valor is None or pvp_valor is None else pl_valor * pvp_valor
    pontuacao = None
    if valor_intrinseco is not None and preco is not None and preco > 0:
        pontuacao = (valor_intrinseco / preco - 1) * 100
    else:
        motivos["margem_seguranca"] = (
            "sem LPA e VPA positivos: elegível, mas sem margem de segurança calculável"
        )

    if indisponiveis:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
            dados={
                "pl": pl_valor,
                "pvp": pvp_valor,
                "pl_pvp": pl_pvp,
                "divida_liquida_patrim": div_patrim,
                "liquidez_corrente": liquidez,
                "valor_intrinseco": valor_intrinseco,
                "valor_intrinseco_estimado": valor_intrinseco,
            },
        )

    return _resultado(
        elegivel=not reprovados,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=ativo,
        dados={
            "pl": pl_valor,
            "pvp": pvp_valor,
            "pl_pvp": pl_pvp,
            "divida_liquida_patrim": div_patrim,
            "liquidez_corrente": liquidez,
            "valor_intrinseco": valor_intrinseco,
            "valor_intrinseco_estimado": valor_intrinseco,
        },
    )


def _dividendos_anuais(ativo: dict[str, Any]) -> list[tuple[int, float]]:
    direto = ativo.get("dividendos_por_ano")
    if isinstance(direto, dict):
        anuais: list[tuple[int, float]] = []
        for ano, valor in direto.items():
            numero = _num(valor)
            try:
                chave = int(ano)
            except (TypeError, ValueError):
                continue
            if numero is not None:
                anuais.append((chave, numero))
        return sorted(anuais)

    historico = ativo.get("proventos") or ativo.get("historico_proventos") or []
    if not isinstance(historico, (list, tuple)) or not historico:
        return []

    nro_acoes = _valor(ativo, "nro_acoes")
    por_acao = bool(ativo.get("proventos_ja_por_acao"))
    por_ano: dict[int, float] = {}
    for item in historico:
        if not isinstance(item, dict):
            continue
        valor = _num(item.get("valor"))
        data = item.get("data_ex") or item.get("data")
        if valor is None or data is None:
            continue
        try:
            ano = int(str(data)[:4])
        except (TypeError, ValueError):
            continue
        if not por_acao and nro_acoes is not None and nro_acoes > 0:
            valor = valor / nro_acoes
        por_ano[ano] = por_ano.get(ano, 0.0) + valor
    return sorted(por_ano.items())


def dividendos_medios_por_acao(
    ativo: dict[str, Any], periodo_anos: int = 5
) -> tuple[float | None, int | None]:
    """Média dos dividendos anuais por ação no período configurável."""

    anos_declarado = _num(ativo.get("anos_dividendos"))
    anos_declarado_int = None if anos_declarado is None else int(anos_declarado)

    direto = _valor(ativo, "dividendos_medios_por_acao")
    if direto is not None:
        return direto, anos_declarado_int

    anuais = _dividendos_anuais(ativo)
    if anuais:
        selecionados = anuais[-int(periodo_anos) :] if periodo_anos > 0 else anuais
        media = _media([valor for _, valor in selecionados])
        return media, anos_declarado_int if anos_declarado_int is not None else len(anuais)

    lista = ativo.get("dividendos_por_acao") or ativo.get("proventos_por_acao")
    if isinstance(lista, (list, tuple)) and lista:
        valores = [numero for numero in (_num(item) for item in lista) if numero is not None]
        if valores:
            selecionados = valores[-int(periodo_anos) :] if periodo_anos > 0 else valores
            anos = anos_declarado_int if anos_declarado_int is not None else len(valores)
            return _media(selecionados), anos

    return None, None


def dividendos_anuais_mediana(
    ativo: dict[str, Any], periodo_anos: int = 5
) -> tuple[float | None, int | None]:
    """Mediana das somas anuais de proventos dos últimos anos com provento.

    Um ano com provento extraordinário não desloca a mediana como deslocaria a
    média. O segundo valor é a quantidade de anos com provento considerada.
    """

    anuais = _dividendos_anuais(ativo)
    if anuais:
        selecionados = anuais[-int(periodo_anos) :] if periodo_anos > 0 else anuais
        com_provento = [(ano, valor) for ano, valor in selecionados if valor > 0]
        if not com_provento:
            return None, 0
        mediana = statistics.median([valor for _, valor in com_provento])
        return float(mediana), len(com_provento)

    anos_declarado = _num(ativo.get("anos_dividendos"))
    anos_declarado_int = None if anos_declarado is None else int(anos_declarado)

    direto = _valor(ativo, "dividendos_medios_por_acao")
    if direto is not None:
        return direto, anos_declarado_int

    lista = ativo.get("dividendos_por_acao") or ativo.get("proventos_por_acao")
    if isinstance(lista, (list, tuple)) and lista:
        valores = [numero for numero in (_num(item) for item in lista) if numero is not None]
        if valores:
            selecionados = valores[-int(periodo_anos) :] if periodo_anos > 0 else valores
            anos = anos_declarado_int if anos_declarado_int is not None else len(valores)
            return float(statistics.median(selecionados)), anos

    return None, None


def bazin(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Preço-teto de Bazin: dividendos médios por ação / taxa exigida."""

    cfg = config.get("bazin", {})
    taxa = _num(cfg.get("taxa_exigida"))
    if taxa is None or taxa <= 0:
        taxa = 0.06
    periodo = int(_num(cfg.get("periodo_dividendos_anos")) or 5)
    minimo = int(_num(cfg.get("min_anos_dividendos")) or 3)
    dy_alerta = _num(cfg.get("dy_alerta"))
    if dy_alerta is None:
        dy_alerta = 12.0
    multiplo = _num(cfg.get("multiplo_provento_alerta"))
    if multiplo is None:
        multiplo = 2.0

    preco = _valor(ativo, "preco")
    media, anos = dividendos_anuais_mediana(ativo, periodo)

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}
    avisos: list[str] = []

    if preco is None:
        indisponiveis.append("preco")
        motivos["preco"] = "sem preço"
    if media is None:
        indisponiveis.append("dividendos")
        motivos["dividendos"] = "sem histórico de dividendos por ação"
    if anos is None:
        indisponiveis.append("historico_dividendos")
        motivos["historico_dividendos"] = "sem histórico de anos de dividendos"
    elif anos < minimo:
        indisponiveis.append("historico_dividendos")
        motivos["historico_dividendos"] = (
            f"apenas {anos} ano(s) de histórico; mínimo {minimo}"
        )

    if indisponiveis:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
            avisos=avisos,
        )

    assert preco is not None and media is not None
    preco_teto = media / taxa
    motivos["preco_teto"] = (
        f"mediana dos dividendos anuais {media:.4f} / taxa exigida {taxa:.2%} "
        f"= R$ {preco_teto:.2f}"
    )
    if preco <= preco_teto:
        aprovados.append("preco_teto")
        motivos["preco_teto"] += "; preço atual abaixo ou igual ao teto"
    else:
        reprovados.append("preco_teto")
        motivos["preco_teto"] += "; preço atual acima do teto"

    dy = _valor(ativo, "dy", "dividend_yield")
    if dy is not None and dy > dy_alerta:
        avisos.append(
            "dividend yield atual acima de 12%: pode ser preço em queda ou "
            "provento extraordinário"
        )
        motivos["aviso_dy_alto"] = avisos[-1]

    ultimo_provento = None
    historico = ativo.get("proventos") or ativo.get("historico_proventos")
    if isinstance(historico, (list, tuple)) and historico:
        itens = []
        for item in historico:
            if not isinstance(item, dict):
                continue
            valor = _num(item.get("valor"))
            if valor is None:
                continue
            data = item.get("data_ex") or item.get("data") or ""
            itens.append((str(data), float(valor)))
        itens.sort(key=lambda par: par[0])
        if itens:
            ultimo_provento = itens[-1][1]
    if ultimo_provento is None:
        anuais = _dividendos_anuais(ativo)
        if anuais:
            ultimo_provento = anuais[-1][1]

    destaque = None
    if (
        ultimo_provento is not None
        and media > 0
        and ultimo_provento > multiplo * media
    ):
        destaque = "último provento acima de 2× a mediana"
        avisos.append(
            "último provento acima de 2× a mediana: pode ser provento extraordinário"
        )
        motivos["aviso_provento_extraordinario"] = avisos[-1]

    pontuacao = (preco_teto / preco - 1) * 100 if preco > 0 else None
    return _resultado(
        elegivel=preco <= preco_teto,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=ativo,
        dados={
            "preco_teto": preco_teto,
            "dividendos_medios_por_acao": media,
            "mediana_dividendos_anuais": media,
            "anos_dividendos": anos,
            "dy": dy,
            "destaque": destaque,
        },
        avisos=avisos,
        destaque=destaque,
    )


def _regularidade_meses(ativo: dict[str, Any]) -> float | None:
    direto = _valor(
        ativo, "meses_com_provento_24m", "regularidade_meses", "meses_com_provento"
    )
    if direto is not None:
        return direto
    historico = ativo.get("proventos") or ativo.get("historico_proventos")
    if not isinstance(historico, (list, tuple)) or not historico:
        return None
    limite = date.today() - timedelta(days=730)
    meses: set[str] = set()
    for item in historico:
        if not isinstance(item, dict):
            continue
        data = item.get("data_ex") or item.get("data")
        if data is None:
            continue
        try:
            quando = date.fromisoformat(str(data)[:10])
        except ValueError:
            continue
        if quando < limite:
            continue
        meses.add(str(data)[:7])
    return float(len(meses))


def _crescimento_proventos(ativo: dict[str, Any]) -> float | None:
    direto = _valor(
        ativo, "crescimento_proventos", "crescimento_dividendos", "crescimento_proventos_pct"
    )
    if direto is not None:
        return direto
    anuais = _dividendos_anuais(ativo)
    if len(anuais) >= 2:
        primeiro = anuais[0][1]
        ultimo = anuais[-1][1]
        if primeiro > 0:
            anos = max(1, anuais[-1][0] - anuais[0][0])
            return ((ultimo / primeiro) ** (1 / anos) - 1) * 100
    return None


def dividendos(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Estratégia de dividendos com pesos configuráveis e alerta de insustentabilidade."""

    cfg = config.get("dividendos", {})
    pesos = cfg.get("pesos", {})
    dy_bom = _num(cfg.get("dy_bom")) or 6.0
    regularidade_min = _num(cfg.get("regularidade_min_meses"))
    if regularidade_min is None:
        regularidade_min = 18.0
    payout_max = _num(cfg.get("payout_max"))
    if payout_max is None:
        payout_max = 80.0
    divida_max = _num(cfg.get("divida_max"))
    if divida_max is None:
        divida_max = 3.0
    roe_bom = _num(cfg.get("roe_bom")) or 12.0

    dy = _valor(ativo, "dy", "dividend_yield")
    regularidade = _regularidade_meses(ativo)
    crescimento = _crescimento_proventos(ativo)
    payout_valor = _valor(ativo, "payout")
    divida = _valor(ativo, "divida_liquida_ebitda")
    roe_valor = _valor(ativo, "roe")

    valores = {
        "dy": dy,
        "regularidade": regularidade,
        "crescimento": crescimento,
        "payout": payout_valor,
        "divida_liquida_ebitda": divida,
        "roe": roe_valor,
    }
    essenciais = ("dy", "regularidade", "payout", "divida_liquida_ebitda", "roe")

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}
    avisos: list[str] = []

    for nome, valor in valores.items():
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = "sem dado"

    if any(nome in indisponiveis for nome in essenciais):
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
        )

    notas = {
        "dy": _normalizar(dy, 0.0, dy_bom * 2),
        "regularidade": _normalizar(regularidade, 0.0, 24.0),
        "crescimento": _normalizar(crescimento, -10.0, 20.0),
        "payout": _normalizar_inverso(payout_valor, 40.0, 120.0),
        "divida_liquida_ebitda": _normalizar_inverso(divida, 1.0, 5.0),
        "roe": _normalizar(roe_valor, 0.0, 20.0),
    }
    pontuacao, _ = _media_ponderada(notas, pesos)

    regras = {
        "dy": (dy is not None and dy >= dy_bom, "DY 12m: " + _com_casas(dy, "%")),
        "regularidade": (
            regularidade is not None and regularidade >= regularidade_min,
            "meses com provento em 24: " + _com_casas(regularidade, casas=0),
        ),
        "crescimento": (
            crescimento is not None and crescimento > 0,
            "crescimento dos proventos: " + _com_casas(crescimento, "%"),
        ),
        "payout": (
            payout_valor is not None and payout_valor <= payout_max,
            "payout: " + _com_casas(payout_valor, "%"),
        ),
        "divida_liquida_ebitda": (
            divida is not None and divida <= divida_max,
            "dívida líquida/EBITDA: " + _com_casas(divida),
        ),
        "roe": (
            roe_valor is not None and roe_valor >= roe_bom,
            "ROE: " + _com_casas(roe_valor, "%"),
        ),
    }
    for nome, (aprovado, texto) in regras.items():
        motivos[nome] = texto
        (aprovados if aprovado else reprovados).append(nome)

    if payout_valor is not None and payout_valor > 100:
        avisos.append("payout acima de 100%: dividendos podem ser insustentáveis")
        motivos["alerta_insustentabilidade"] = avisos[-1]
    if divida is not None and divida > 3:
        avisos.append(
            "dívida líquida/EBITDA acima de 3: dividendos podem ser insustentáveis"
        )
        motivos["alerta_insustentabilidade"] = avisos[-1]

    return _resultado(
        elegivel=True,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=ativo,
        dados={**valores, "notas": notas},
        avisos=avisos,
    )


def _desvio_roe(ativo: dict[str, Any]) -> float | None:
    historico = ativo.get("historico_roes") or ativo.get("roes_historicos")
    if not isinstance(historico, (list, tuple)):
        return None
    valores = [numero for numero in (_num(item) for item in historico) if numero is not None]
    if len(valores) < 3:
        return None
    return float(statistics.pstdev(valores))


def qualidade_crescimento(ativo: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Qualidade do negócio e crescimento, com conversão de lucro em caixa."""

    cfg = config.get("qualidade_crescimento", {})
    pesos = cfg.get("pesos", {})
    divida_max = _num(cfg.get("divida_max"))
    if divida_max is None:
        divida_max = 3.0
    estabilidade_max = _num(cfg.get("estabilidade_max"))
    if estabilidade_max is None:
        estabilidade_max = 5.0

    roic_valor = _valor(ativo, "roic")
    roe_valor = _valor(ativo, "roe")
    margem_valor = _valor(ativo, "margem_liquida")
    crescimento = _valor(
        ativo, "crescimento_receita_5a", "cres_rec_5a", "crescimento_receita"
    )
    divida = _valor(ativo, "divida_liquida_ebitda")
    desvio = _desvio_roe(ativo)

    fluxo = _valor(ativo, "fluxo_caixa_operacional", "fco", "caixa_operacional")
    lucro = _valor(ativo, "lucro_12m", "lucro_liquido")
    conversao = None
    conversao_motivo = "fonte não fornece fluxo de caixa"
    if fluxo is not None and lucro is not None and lucro > 0:
        conversao = fluxo / lucro * 100
        conversao_motivo = f"fluxo de caixa operacional / lucro = {conversao:.1f}%"

    valores = {
        "roic": roic_valor,
        "roe": roe_valor,
        "margem_liquida": margem_valor,
        "crescimento_receita_5a": crescimento,
        "conversao_caixa": conversao,
        "divida_liquida_ebitda": divida,
        "estabilidade_roe": desvio,
    }
    essenciais = ("roic", "roe", "margem_liquida")

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    for nome, valor in valores.items():
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = (
                conversao_motivo if nome == "conversao_caixa" else "sem dado"
            )

    if any(nome in indisponiveis for nome in essenciais):
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=ativo,
        )

    notas = {
        "roic": _normalizar(roic_valor, 0.0, 20.0),
        "roe": _normalizar(roe_valor, 0.0, 20.0),
        "margem_liquida": _normalizar(margem_valor, 0.0, 20.0),
        "crescimento_receita_5a": _normalizar(crescimento, -5.0, 20.0),
        "conversao_caixa": _normalizar(conversao, 0.0, 100.0),
        "divida_liquida_ebitda": _normalizar_inverso(divida, 0.0, divida_max * 2),
        "estabilidade_roe": _normalizar_inverso(desvio, 0.0, estabilidade_max * 2),
    }
    pontuacao, _ = _media_ponderada(notas, pesos)

    regras = {
        "roic": (roic_valor is not None and roic_valor >= 10, f"ROIC: {roic_valor}%"),
        "roe": (roe_valor is not None and roe_valor >= 12, f"ROE: {roe_valor}%"),
        "margem_liquida": (
            margem_valor is not None and margem_valor >= 10,
            f"margem líquida: {margem_valor}%",
        ),
        "crescimento_receita_5a": (
            crescimento is not None and crescimento > 0,
            f"crescimento de receita 5a: {crescimento}%",
        ),
        "conversao_caixa": (
            conversao is not None and conversao >= 80,
            conversao_motivo,
        ),
        "divida_liquida_ebitda": (
            divida is not None and divida <= divida_max,
            "dívida líquida/EBITDA: " + _com_casas(divida),
        ),
        "estabilidade_roe": (
            desvio is not None and desvio <= estabilidade_max,
            f"desvio dos ROEs: {desvio}",
        ),
    }
    for nome, (aprovado, texto) in regras.items():
        if nome not in indisponiveis:
            motivos[nome] = texto
            (aprovados if aprovado else reprovados).append(nome)

    return _resultado(
        elegivel=True,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=ativo,
        dados={**valores, "notas": notas},
    )


def avaliar_fii(campos: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """FIIs: DY, P/VP, liquidez e regularidade. Não usa ROE de empresa."""

    cfg = config.get("fiis", {})
    pesos = cfg.get("pesos", {})
    dy_min = _num(cfg.get("dy_min"))
    if dy_min is None:
        dy_min = _num(cfg.get("dy_bom")) or 8.0
    dy_bom = _num(cfg.get("dy_bom")) or dy_min
    liquidez_min = _num(cfg.get("liquidez_min")) or 5_000.0

    dy = _valor(campos, "dy", "dividend_yield")
    if dy is None:
        dy = _valor(campos, "dy_12m_calculado")
    pvp_valor = _valor(campos, "pvp")
    liquidez = _valor(
        campos, "liquidez_diaria_brl", "liquidez_2meses", "liquidez", "volume"
    )
    regularidade = _regularidade_meses(campos)

    valores = {
        "dy": dy,
        "pvp": pvp_valor,
        "liquidez": liquidez,
        "regularidade": regularidade,
    }

    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    for nome, valor in valores.items():
        if valor is not None:
            continue
        if nome == "pvp":
            indisponiveis.append("p_vp")
            motivos["p_vp"] = "fonte não entrega valor patrimonial para este FII"
        else:
            indisponiveis.append(nome)
            motivos[nome] = "sem dado"
    for nome in ("vacancia", "inadimplencia"):
        if _valor(campos, nome) is None:
            indisponiveis.append(nome)
            motivos[nome] = "fonte não fornece"

    if dy is None or liquidez is None:
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=campos,
        )

    notas = {
        "dy": _normalizar(dy, 0.0, dy_bom * 2),
        "pvp": _normalizar_inverso(pvp_valor, 0.8, 1.5),
        "liquidez": _normalizar(liquidez, 0.0, liquidez_min * 4),
        "regularidade": _normalizar(regularidade, 0.0, 24.0),
    }
    pontuacao, _ = _media_ponderada(notas, pesos)

    regras = {
        "dy": (dy >= dy_min, "DY 12m: " + _com_casas(dy, "%")),
        "liquidez": (
            liquidez >= liquidez_min,
            _texto_liquidez(liquidez, liquidez_min),
        ),
        "regularidade": (
            regularidade is not None and regularidade >= 18,
            "meses com provento em 24: " + _com_casas(regularidade, casas=0),
        ),
    }
    if pvp_valor is not None:
        regras["pvp"] = (pvp_valor <= 1.0, "P/VP: " + _com_casas(pvp_valor))
    for nome, (aprovado, texto) in regras.items():
        motivos[nome] = texto
        (aprovados if aprovado else reprovados).append(nome)

    elegivel = dy >= dy_min and liquidez >= liquidez_min
    return _resultado(
        elegivel=elegivel,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=campos,
        dados={
            **valores,
            "liquidez_diaria_brl": liquidez,
            "notas": notas,
            "pvp_origem": campos.get("pvp_origem"),
            "taxa_administracao_informe": _valor(
                campos, "taxa_administracao_informe"
            ),
            "taxa_administracao_campo": campos.get("taxa_administracao_campo")
            or "Percentual_Despesas_Taxa_Administracao",
        },
    )


def _retorno_e_volatilidade(campos: dict[str, Any]) -> tuple[float | None, float | None]:
    retorno = _valor(campos, "retorno", "retorno_12m")
    volatilidade = _valor(campos, "volatilidade", "volatilidade_12m")
    if retorno is not None and volatilidade is not None:
        return retorno, volatilidade

    cotacoes = campos.get("cotacoes") or campos.get("historico")
    if isinstance(cotacoes, dict):
        cotacoes = cotacoes.get("fechamento")
    if isinstance(cotacoes, (list, tuple)) and cotacoes:
        valores = [numero for numero in (_num(item) for item in cotacoes) if numero is not None]
    else:
        valores = []
    if len(valores) < 2:
        return retorno, volatilidade

    if retorno is None and valores[0] > 0:
        retorno = (valores[-1] / valores[0] - 1) * 100
    if volatilidade is None and len(valores) >= 3:
        retornos = [
            valores[i] / valores[i - 1] - 1
            for i in range(1, len(valores))
            if valores[i - 1] > 0
        ]
        if len(retornos) >= 2:
            desvio = statistics.pstdev(retornos)
            volatilidade = desvio * math.sqrt(252) * 100
    return retorno, volatilidade


def avaliar_etf(campos: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """ETFs: liquidez, retorno e volatilidade. Taxa de administração indisponível."""

    cfg = config.get("etfs", {})
    pesos = cfg.get("pesos", {})
    liquidez_min = _num(cfg.get("liquidez_min")) or 50_000.0
    vol_max = _num(cfg.get("volatilidade_max")) or 35.0
    vol_min = _num(cfg.get("volatilidade_min")) or 5.0

    liquidez = _valor(
        campos, "liquidez_diaria_brl", "liquidez_2meses", "liquidez", "volume"
    )
    retorno, volatilidade = _retorno_e_volatilidade(campos)

    valores = {"liquidez": liquidez, "retorno": retorno, "volatilidade": volatilidade}
    aprovados: list[str] = []
    reprovados: list[str] = []
    indisponiveis: list[str] = []
    motivos: dict[str, str] = {}

    for nome, valor in valores.items():
        if valor is None:
            indisponiveis.append(nome)
            motivos[nome] = "sem dado"
    indisponiveis.append("taxa_administracao")
    motivos["taxa_administracao"] = "fonte não fornece"

    if any(nome in indisponiveis for nome in valores):
        return _resultado(
            elegivel=False,
            pontuacao=None,
            aprovados=aprovados,
            reprovados=reprovados,
            indisponiveis=indisponiveis,
            motivos=motivos,
            ativo=campos,
        )

    notas = {
        "liquidez": _normalizar(liquidez, 0.0, liquidez_min * 4),
        "retorno": _normalizar(retorno, -20.0, 30.0),
        "volatilidade": _normalizar_inverso(volatilidade, vol_min, vol_max * 2),
    }
    pontuacao, _ = _media_ponderada(notas, pesos)

    regras = {
        "liquidez": (
            liquidez is not None and liquidez >= liquidez_min,
            _texto_liquidez(liquidez, liquidez_min)
            if liquidez is not None
            else "sem dado",
        ),
        "retorno": (retorno is not None and retorno > 0, f"retorno: {retorno}%"),
        "volatilidade": (
            volatilidade is not None and volatilidade <= vol_max,
            f"volatilidade anualizada: {volatilidade}%",
        ),
    }
    for nome, (aprovado, texto) in regras.items():
        motivos[nome] = texto
        (aprovados if aprovado else reprovados).append(nome)

    return _resultado(
        elegivel=True,
        pontuacao=pontuacao,
        aprovados=aprovados,
        reprovados=reprovados,
        indisponiveis=indisponiveis,
        motivos=motivos,
        ativo=campos,
        dados={**valores, "liquidez_diaria_brl": liquidez, "notas": notas},
    )


def tem_fundamentos_empresa(ativo: dict[str, Any]) -> bool:
    """BDR só entra nas estratégias de ação se os fundamentos da fonte existirem."""

    campos = ("pl", "pvp", "lpa", "vpa", "vpa_calculado", "roe", "lucro_12m")
    return any(_num(ativo.get(campo)) is not None for campo in campos)


ESTRATEGIAS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "magic_formula": magic_formula,
    "earnings_yield": earnings_yield,
    "upside_consenso": upside_consenso,
    "pl_roe": pl_roe,
    "graham": graham,
    "bazin": bazin,
    "dividendos": dividendos,
    "qualidade": qualidade_crescimento,
    "qualidade_crescimento": qualidade_crescimento,
    "fiis": avaliar_fii,
    "etfs": avaliar_etf,
}


def salvar_config(nome: str, config: dict[str, Any] | None = None) -> None:
    """Grava a configuração em ``configuracoes`` com a chave ``estrategia.<nome>``."""

    from app import db

    db.gravar_configuracao(f"estrategia.{nome}", config or CONFIG_PADRAO)


def carregar_config(nome: str) -> dict[str, Any]:
    from app import db

    config = db.ler_configuracao(f"estrategia.{nome}")
    return config if isinstance(config, dict) else CONFIG_PADRAO
