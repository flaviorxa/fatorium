"""Adaptador Fundamentus (somente ações da B3 e FIIs)."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, TypeVar

import fundamentus
import pandas as pd

from app import db

T = TypeVar("T")

# Ritmo mínimo entre requisições ao Fundamentus. A concorrência do coletor
# continua, mas a fonte é serializada para não tomar bloqueio.
INTERVALO_MIN_S = 0.35
ESPERA_RATE_LIMIT_S = 5.0
TENTATIVAS_RATE_LIMIT = 3

_LOCK = threading.Lock()
_ULTIMA_REQUISICAO = 0.0


def _erro_de_ritmo(exc: BaseException) -> bool:
    resposta = getattr(exc, "response", None)
    status = getattr(resposta, "status_code", None)
    if status in (403, 429):
        return True
    texto = str(exc)
    return "429" in texto or "403" in texto


def _requisitar(fn: Callable[..., T], *args: Any) -> T:
    """Executa uma chamada à fonte respeitando o ritmo e retentando bloqueios."""

    global _ULTIMA_REQUISICAO
    with _LOCK:
        agora = time.monotonic()
        espera = INTERVALO_MIN_S - (agora - _ULTIMA_REQUISICAO)
        if espera > 0:
            time.sleep(espera)

        for tentativa in range(TENTATIVAS_RATE_LIMIT + 1):
            try:
                return fn(*args)
            except Exception as exc:  # noqa: BLE001 - a fonte decide o que propagar
                if _erro_de_ritmo(exc) and tentativa < TENTATIVAS_RATE_LIMIT:
                    time.sleep(ESPERA_RATE_LIMIT_S * (2**tentativa))
                    continue
                raise
            finally:
                _ULTIMA_REQUISICAO = time.monotonic()
        raise RuntimeError("requisição ao Fundamentus esgotou as tentativas")


# Campos canônicos -> coluna real devolvida por fundamentus.get_papel().
MAPA_CAMPOS = {
    "preco": "Cotacao",
    "pl": "PL",
    "pvp": "PVP",
    "dy": "Div_Yield",
    "ev_ebitda": "EV_EBITDA",
    "ev_ebit": "EV_EBIT",
    "roe": "ROE",
    "roic": "ROIC",
    "margem_bruta": "Marg_Bruta",
    "margem_ebit": "Marg_EBIT",
    "margem_liquida": "Marg_Liquida",
    "lpa": "LPA",
    "divida_liquida": "Div_Liquida",
    "divida_bruta": "Div_Bruta",
    "caixa": "Disponibilidades",
    "divida_liquida_patrim": "Div_Liq_Patrim",
    "patrimonio_liquido": "Patrim_Liq",
    "receita_12m": "Receita_Liquida_12m",
    "lucro_12m": "Lucro_Liquido_12m",
    "ebit_12m": "EBIT_12m",
    "crescimento_receita_5a": "Cres_Rec_5a",
    "nro_acoes": "Nro_Acoes",
    "liquidez_corrente": "Liquidez_Corr",
    "liquidez_diaria_brl": "Vol_med_2m",
    "max_52s": "Max_52_sem",
    "min_52s": "Min_52_sem",
    "empresa": "Empresa",
    "setor": "Setor",
    "subsetor": "Subsetor",
    "data_cotacao": "Data_ult_cot",
}

# Colunas que o fundamentus 0.3.2 passa por utils.fmt_dec(). Com pandas 3.1 o
# ponto decimal de valores float é removido (5.07 -> "507"); detectamos e
# corrigimos lendo a tabela HTML bruta, sem inventar valores.
_COLUNAS_FMT_DEC = {
    "PL",
    "PVP",
    "PEBIT",
    "PSR",
    "PAtivos",
    "PCap_Giro",
    "PAtiv_Circ_Liq",
    "Div_Yield",
    "EV_EBITDA",
    "EV_EBIT",
    "Cres_Rec_5a",
    "LPA",
    "VPA",
    "Marg_Bruta",
    "Marg_EBIT",
    "Marg_Liquida",
    "EBIT_Ativo",
    "ROIC",
    "ROE",
    "Liquidez_Corr",
    "Div_Liq_Patrim",
    "Giro_Ativos",
}

_ROTULOS_BRUTOS = {
    "P/L": "pl",
    "P/VP": "pvp",
    "Cresc. Rec. 5a": "crescimento_receita_5a",
    "Div. Yield": "dy",
    "EV / EBITDA": "ev_ebitda",
    "EV / EBIT": "ev_ebit",
    "LPA": "lpa",
    "Marg. Bruta": "margem_bruta",
    "Marg. EBIT": "margem_ebit",
    "Marg. Líquida": "margem_liquida",
    "ROIC": "roic",
    "ROE": "roe",
    "Liquidez Corr": "liquidez_corrente",
    "Dív Líq / Patrim": "divida_liquida_patrim",
}

_CAMPOS_TEXTO = {"empresa", "setor", "subsetor", "data_cotacao"}


def para_numero(valor: Any) -> float | None:
    """Converte números do Fundamentus/planilhas para ``float``.

    Aceita ``"7,30%"`` -> ``7.3``, ``"1.234.567,89"`` -> ``1234567.89`` e
    ``"-"``/vazio -> ``None``. Nunca devolve zero para valor ausente.
    """

    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = float(valor)
        return None if pd.isna(numero) else numero

    texto = str(valor).strip()
    if not texto or texto in {"-", "--", "N/A", "n/a"}:
        return None

    negativo = False
    if texto.startswith("(") and texto.endswith(")"):
        negativo = True
        texto = texto[1:-1]

    texto = texto.replace("%", "").replace("R$", "").replace(" ", "")
    texto = texto.replace("\u00a0", "")
    if not texto or texto in {"-", "--"}:
        return None

    if "," in texto:
        # Formato brasileiro: ponto é separador de milhar.
        texto = texto.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", texto):
        # Apenas pontos, mas em grupos de milhar (ex.: "1.234.567").
        texto = texto.replace(".", "")

    try:
        numero = float(texto)
    except ValueError:
        return None
    if pd.isna(numero):
        return None
    return -numero if negativo else numero


converter_numero = para_numero


def _parece_corrompido(valor: Any) -> bool:
    if not isinstance(valor, str):
        return False
    texto = valor.strip()
    return re.fullmatch(r"-?\d{2,}", texto) is not None


def _ler_indicadores_brutos(ticker: str) -> dict[str, float]:
    """Relê a tabela de indicadores do HTML, preservando os decimais."""

    try:
        tabelas = _requisitar(fundamentus.get_detalhes_raw, ticker)
    except Exception:  # noqa: BLE001 - correção é best-effort
        return {}

    if not tabelas or len(tabelas) < 3:
        return {}

    tabela = tabelas[2]
    corrigidos: dict[str, float] = {}
    for _, linha in tabela.iterrows():
        for col_rotulo, col_valor in ((0, 1), (2, 3), (4, 5)):
            if col_rotulo >= len(tabela.columns) or col_valor >= len(tabela.columns):
                continue
            rotulo = str(linha.iloc[col_rotulo]).replace("?", "").strip()
            campo = _ROTULOS_BRUTOS.get(rotulo)
            if campo is None:
                continue
            numero = para_numero(linha.iloc[col_valor])
            if numero is not None:
                corrigidos[campo] = numero
    return corrigidos


def _pares_de_rotulos(tabela: pd.DataFrame, colunas: int = 6) -> list[tuple[str, Any]]:
    pares: list[tuple[str, Any]] = []
    for _, linha in tabela.iterrows():
        for col_rotulo in range(0, colunas, 2):
            col_valor = col_rotulo + 1
            if col_rotulo >= len(tabela.columns) or col_valor >= len(tabela.columns):
                continue
            rotulo = str(linha.iloc[col_rotulo]).replace("?", "").strip()
            if rotulo and rotulo.lower() != "nan":
                pares.append((rotulo, linha.iloc[col_valor]))
    return pares


def _ler_fii(ticker: str) -> dict[str, Any]:
    """Fallback para FIIs: o get_papel() só entende o layout de ações."""

    try:
        tabelas = _requisitar(fundamentus.get_detalhes_raw, ticker)
    except Exception:  # noqa: BLE001
        return {}
    if not tabelas or len(tabelas) < 3:
        return {}

    rotulos: dict[str, Any] = {}
    for indice in (0, 1, 2):
        for rotulo, valor in _pares_de_rotulos(tabelas[indice]):
            rotulos.setdefault(rotulo, valor)

    campos: dict[str, Any] = {}
    mapa = {
        "Cotação": "preco",
        "Div. Yield": "dy",
        "Min 52 sem": "min_52s",
        "Max 52 sem": "max_52s",
        "Nro. Cotas": "nro_acoes",
    }
    for rotulo, canonico in mapa.items():
        if rotulo in rotulos:
            numero = para_numero(rotulos[rotulo])
            if numero is not None:
                campos[canonico] = numero

    nome = rotulos.get("Nome")
    if nome is not None and not (isinstance(nome, float) and pd.isna(nome)):
        texto = str(nome).strip()
        if texto and texto.lower() != "nan":
            campos["empresa"] = texto

    data = db.data_iso(rotulos.get("Data últ cot"))
    if data is not None:
        campos["data_cotacao"] = data

    return campos


def _extrair_texto(valor: Any) -> str | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    texto = str(valor).strip()
    return texto or None


def papel(ticker: str, classe: str = "acao_b3") -> tuple[dict[str, Any], list[str]]:
    """Dados fundamentalistas de um papel brasileiro.

    Devolve ``(campos, avisos)`` com nomes canônicos. Levanta ``ValueError``
    para classes que esta fonte não cobre (cripto, BDR, ETF).
    """

    if classe not in {"acao_b3", "fii"}:
        raise ValueError(f"fonte não cobre {classe}")

    avisos: list[str] = []
    try:
        df = _requisitar(fundamentus.get_papel, ticker)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"falha ao consultar Fundamentus: {type(exc).__name__}: {exc}"]

    if df is None or len(df) == 0:
        if classe == "fii":
            campos_fii = _ler_fii(ticker)
            if campos_fii:
                avisos = [
                    "Fundamentus não expõe o conjunto completo de ações para FII; "
                    "campos ausentes foram omitidos"
                ]
                patrimonio = campos_fii.get("patrimonio_liquido")
                nro_cotas = campos_fii.get("nro_acoes")
                if patrimonio is not None and nro_cotas:
                    campos_fii["vpa_calculado"] = patrimonio / nro_cotas
                return campos_fii, avisos
        return {}, [f"Fundamentus não devolveu dados para {ticker}"]

    linha = df.iloc[0]
    colunas = set(linha.index)

    suspeito = any(
        coluna in colunas and _parece_corrompido(linha.get(coluna))
        for coluna in _COLUNAS_FMT_DEC
    )
    corrigidos: dict[str, float] = {}
    if suspeito:
        corrigidos = _ler_indicadores_brutos(ticker)
        if corrigidos:
            avisos.append("decimais dos indicadores corrigidos pela tabela HTML do Fundamentus")
        else:
            avisos.append("não foi possível corrigir os decimais dos indicadores")

    campos: dict[str, Any] = {}
    for canonico, coluna in MAPA_CAMPOS.items():
        if canonico in corrigidos:
            campos[canonico] = corrigidos[canonico]
            continue
        if coluna not in colunas:
            avisos.append(f"sem {coluna}")
            continue

        valor = linha.get(coluna)
        if canonico in _CAMPOS_TEXTO:
            if canonico == "data_cotacao":
                texto = db.data_iso(valor)
                if texto is None:
                    avisos.append(f"sem {coluna}")
                    continue
                campos[canonico] = texto
            else:
                texto = _extrair_texto(valor)
                if texto is None:
                    avisos.append(f"sem {coluna}")
                    continue
                campos[canonico] = texto
            continue

        numero = para_numero(valor)
        if numero is None:
            avisos.append(f"sem {coluna}")
            continue
        campos[canonico] = numero

    patrimonio = campos.get("patrimonio_liquido")
    nro_acoes = campos.get("nro_acoes")
    if patrimonio is not None and nro_acoes is not None and nro_acoes > 0:
        campos["vpa_calculado"] = patrimonio / nro_acoes
    else:
        avisos.append("sem patrimônio líquido/número de ações para VPA")

    return campos, avisos
