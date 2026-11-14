"""Edição da lista oficial de ativos em Markdown.

A lista em Markdown é a única fonte de verdade do universo. Este módulo lê e
edita as linhas originais do arquivo (sem reconstruí-lo por inteiro), grava um
backup antes de cada escrita e usa ``os.replace`` para publicar o novo conteúdo
de forma atômica.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app import config

CLASSES_VALIDAS = ("acao_b3", "bdr", "etf", "fii", "cripto")

NOME_SECAO: dict[str, str] = {
    "cripto": "Criptomoedas",
    "acao_b3": "Ações",
    "bdr": "BDRs",
    "etf": "ETFs",
    "fii": "Fundos Imobiliários",
}

# Ordem canônica das seções, usada apenas ao criar uma seção ausente.
ORDEM_SECOES = ("cripto", "acao_b3", "bdr", "etf", "fii")

DIRETORIO_BACKUPS = config.RAIZ / "data" / "backups"
MAX_BACKUPS = 20

_RE_SECAO = re.compile(r"^(##\s+)(?P<titulo>.*?)(?:\s*\((?P<contagem>\d+)\))?\s*$")
_RE_ATUALIZADO = re.compile(
    r"^(>\s*Atualizado:\s*)(?P<data>\d{2}/\d{2}/\d{4})(?P<traco>\s*—\s*)"
    r"(?P<motivo>.*?)(?P<fim>\r?\n)?$"
)
_RE_TICKER_B3 = re.compile(r"\b[A-Z][A-Z0-9]{3}\d{1,2}\b")
_RE_CRIPTO_BASE = re.compile(r"^[A-Z0-9]{2,10}$")
_RE_CRIPTO = re.compile(r"^[A-Z0-9]{2,10}-[A-Z]{2,5}$")
_RE_ACAO = re.compile(r"^[A-Z]{4}\d{1,2}$")
_RE_BDR_ETF_FII = re.compile(r"^[A-Z]{4}\d{2}$")


def _sem_acento(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def _classe_da_secao(titulo: str) -> str | None:
    chave = _sem_acento(titulo).strip().casefold()
    chave = re.sub(r"\s+", " ", chave)
    if chave.startswith("criptomoeda"):
        return "cripto"
    if chave == "acoes":
        return "acao_b3"
    if chave in {"bdr", "bdrs", "brazilian depositary receipts"}:
        return "bdr"
    if chave in {"etf", "etfs"}:
        return "etf"
    if chave.startswith("fundos imobiliarios"):
        return "fii"
    return None


def caminho_lista() -> Path:
    """Caminho da lista, com ``INVEST_LISTA_ATIVOS`` tendo prioridade."""

    return Path(os.getenv("INVEST_LISTA_ATIVOS") or config.LISTA_ATIVOS)


def _ler_texto(arquivo: Path | None = None) -> str:
    caminho = Path(arquivo) if arquivo is not None else caminho_lista()
    if not caminho.exists():
        raise FileNotFoundError(
            f"lista de ativos não encontrada em {caminho}; "
            "ajuste INVEST_LISTA_ATIVOS ou crie o arquivo"
        )
    return caminho.read_text(encoding="utf-8")


def _fim_linha(texto: str) -> str:
    return "\r\n" if "\r\n" in texto else "\n"


def _separar_fim(linha: str) -> tuple[str, str]:
    fim = ""
    if linha.endswith("\r\n"):
        fim = "\r\n"
        linha = linha[:-2]
    elif linha.endswith("\n"):
        fim = "\n"
        linha = linha[:-1]
    return linha, fim


def _tickers_da_linha(linha: str, classe: str) -> list[str]:
    """Extrai os tickers de uma linha de seção, preservando a ordem."""

    conteudo, _ = _separar_fim(linha)
    conteudo = conteudo.strip()
    if conteudo.startswith(("- ", "* ")):
        conteudo = conteudo[2:].strip()

    if classe == "cripto":
        tickers: list[str] = []
        for parte in conteudo.split(","):
            parte = parte.strip().lstrip("-* ").strip()
            # Descrições aparecem depois de "—" ou " - " no formato do vault.
            parte = parte.split("—")[0].split(" - ")[0].strip()
            if not parte:
                continue
            token = parte.split()[0].upper()
            if _RE_CRIPTO.match(token):
                ticker = token
            elif _RE_CRIPTO_BASE.match(token):
                ticker = f"{token}-BRL"
            else:
                continue
            if ticker not in tickers:
                tickers.append(ticker)
        return tickers

    tickers = []
    for achado in _RE_TICKER_B3.findall(conteudo.upper()):
        if achado not in tickers:
            tickers.append(achado)
    return tickers


def _analisar(texto: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Devolve as linhas originais e a descrição das seções encontradas."""

    linhas = texto.splitlines(keepends=True)
    secoes: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None

    for indice, linha in enumerate(linhas):
        conteudo, _ = _separar_fim(linha)
        limpo = conteudo.strip()

        if limpo == "---":
            if atual is not None:
                atual["fim"] = indice
                atual = None
            continue

        achado = _RE_SECAO.match(conteudo)
        if achado:
            if atual is not None:
                atual["fim"] = indice
            atual = {
                "indice": indice,
                "titulo": achado.group("titulo").strip(),
                "classe": _classe_da_secao(achado.group("titulo")),
                "linha_conteudo": None,
                "fim": len(linhas),
            }
            secoes.append(atual)
            continue

        if atual is None:
            continue
        if not limpo or limpo.startswith(">") or limpo.startswith("#"):
            continue
        if atual["linha_conteudo"] is None:
            atual["linha_conteudo"] = indice

    return linhas, secoes


def ler() -> dict[str, dict[str, Any]]:
    """Lê as seções e devolve ``{ticker: {classe, secao, indice}}``."""

    linhas, secoes = _analisar(_ler_texto())
    universo: dict[str, dict[str, Any]] = {}
    for secao in secoes:
        classe = secao.get("classe")
        if classe is None or secao.get("linha_conteudo") is None:
            continue
        linha = linhas[int(secao["linha_conteudo"])]
        for posicao, ticker in enumerate(_tickers_da_linha(linha, classe)):
            universo[ticker] = {
                "classe": classe,
                "secao": secao["titulo"],
                "indice": posicao,
            }
    return universo


def validar_ticker(ticker: str, classe: str) -> str:
    """Normaliza e valida o ticker para a classe informada."""

    if classe not in CLASSES_VALIDAS:
        raise ValueError(
            f"classe inválida: {classe!r}; use uma de {', '.join(CLASSES_VALIDAS)}"
        )

    normalizado = str(ticker or "").strip().upper()
    if not normalizado:
        raise ValueError("ticker inválido: informe um ticker")

    if classe == "cripto":
        # Par em reais: o site exibe tudo em R$, o Yahoo só negocia em dólar.
        base = normalizado.split("-")[0]
        valido = _RE_CRIPTO_BASE.match(base) and (
            normalizado == base or _RE_CRIPTO.match(normalizado)
        )
        if not valido:
            raise ValueError(
                f"ticker inválido para cripto: {normalizado!r}; "
                "use algo como BTC-BRL"
            )
        return f"{base}-BRL"

    if classe == "acao_b3":
        if not _RE_ACAO.match(normalizado):
            raise ValueError(
                f"ticker inválido para ação da B3: {normalizado!r}; "
                "use quatro letras seguidas de 1 ou 2 dígitos (ex.: PETR4)"
            )
        return normalizado

    if not _RE_BDR_ETF_FII.match(normalizado):
        raise ValueError(
            f"ticker inválido para {classe}: {normalizado!r}; "
            "use quatro letras seguidas de dois dígitos (ex.: IVVB11)"
        )
    return normalizado


def _atualizar_contagem(linha: str, contagem: int) -> str:
    conteudo, fim = _separar_fim(linha)
    if re.search(r"\(\d+\)\s*$", conteudo):
        conteudo = re.sub(r"\(\d+\)\s*$", f"({contagem})", conteudo)
    else:
        conteudo = f"{conteudo.rstrip()} ({contagem})"
    return conteudo + fim


def _atualizar_linha_atualizado(linhas: list[str], motivo: str) -> None:
    hoje = date.today().strftime("%d/%m/%Y")
    for indice, linha in enumerate(linhas):
        achado = _RE_ATUALIZADO.match(linha)
        if not achado:
            continue
        extras = ""
        restante = achado.group("motivo")
        if " · " in restante:
            extras = " · " + " · ".join(restante.split(" · ")[1:])
        linhas[indice] = (
            f"{achado.group(1)}{hoje}{achado.group('traco')}"
            f"{motivo}{extras}{achado.group('fim') or ''}"
        )
        return

    # Não havia linha de atualização: cria uma logo depois do título.
    fim = _fim_linha("".join(linhas))
    for indice, linha in enumerate(linhas):
        if linha.startswith("# "):
            linhas.insert(
                indice + 1, f"> Atualizado: {hoje} — {motivo}{fim}"
            )
            return


def _preparar_backup(texto: str) -> Path:
    DIRETORIO_BACKUPS.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = DIRETORIO_BACKUPS / f"lista-ativos-{carimbo}.md"
    destino.write_text(texto, encoding="utf-8")

    backups = sorted(DIRETORIO_BACKUPS.glob("lista-ativos-*.md"))
    for antigo in backups[:-MAX_BACKUPS]:
        try:
            antigo.unlink()
        except FileNotFoundError:  # pragma: no cover - corrida entre processos
            pass
    return destino


def _gravar_atomico(arquivo: Path, texto: str) -> None:
    modo = stat.S_IMODE(arquivo.stat().st_mode)
    descritor, temporario = tempfile.mkstemp(
        prefix=f".{arquivo.name}.", suffix=".tmp", dir=arquivo.parent
    )
    try:
        with os.fdopen(descritor, "w", encoding="utf-8", newline="") as saida:
            saida.write(texto)
        os.chmod(temporario, modo)
        os.replace(temporario, arquivo)
    finally:
        if os.path.exists(temporario):
            os.unlink(temporario)


def _publicar(arquivo: Path, original: str, novo: str) -> None:
    _preparar_backup(original)
    _gravar_atomico(arquivo, novo)


def _secao_da_classe(
    secoes: list[dict[str, Any]], classe: str
) -> dict[str, Any] | None:
    for secao in secoes:
        if secao.get("classe") == classe:
            return secao
    return None


def _linha_com_ticker(
    linha: str, classe: str, ticker: str
) -> str:
    conteudo, fim = _separar_fim(linha)
    base = conteudo.rstrip()
    if not base:
        return f"{ticker}{fim}"
    if base.endswith(","):
        return f"{base} {ticker}{fim}"
    return f"{base}, {ticker}{fim}"


def incluir(ticker: str, classe: str) -> dict[str, Any]:
    """Inclui o ticker na seção da classe e corrige a contagem do cabeçalho."""

    normalizado = validar_ticker(ticker, classe)
    arquivo = caminho_lista()
    original = _ler_texto(arquivo)
    universo = ler()

    if normalizado in universo:
        raise ValueError(f"{normalizado} já está na lista")

    linhas, secoes = _analisar(original)
    fim = _fim_linha(original)
    secao = _secao_da_classe(secoes, classe)

    if secao is None:
        titulo = NOME_SECAO[classe]
        bloco = [f"## {titulo} (1){fim}", f"{normalizado}{fim}", fim]
        indice_obs = next(
            (i for i, linha in enumerate(linhas) if linha.strip() == "---"),
            len(linhas),
        )
        if indice_obs > 0 and linhas[indice_obs - 1].strip():
            bloco.insert(0, fim)
        linhas[indice_obs:indice_obs] = bloco
        contagem = 1
        titulo_secao = titulo
    else:
        indice_secao = int(secao["indice"])
        indice_conteudo = secao.get("linha_conteudo")
        if indice_conteudo is None:
            indice_conteudo = indice_secao + 1
            linhas.insert(indice_conteudo, f"{normalizado}{fim}")
        else:
            linhas[int(indice_conteudo)] = _linha_com_ticker(
                linhas[int(indice_conteudo)], classe, normalizado
            )
        contagem = len(
            _tickers_da_linha(linhas[int(indice_conteudo)], classe)
        )
        linhas[indice_secao] = _atualizar_contagem(
            linhas[indice_secao], contagem
        )
        titulo_secao = secao["titulo"]

    _atualizar_linha_atualizado(linhas, "incluído pela tela")
    _publicar(arquivo, original, "".join(linhas))

    return {
        "ticker": normalizado,
        "classe": classe,
        "secao": titulo_secao,
        "contagem": contagem,
        "acao": "incluido",
    }


def _remover_da_linha(linha: str, classe: str, ticker: str) -> str:
    conteudo, fim = _separar_fim(linha)
    prefixo = ""
    achado = re.match(r"^(\s*[-*]\s+)", conteudo)
    if achado:
        prefixo = achado.group(1)
        conteudo = conteudo[achado.end():]

    restantes: list[str] = []
    for parte in conteudo.split(","):
        pedaco = parte.strip()
        if not pedaco:
            continue
        if ticker in _tickers_da_linha(pedaco, classe):
            continue
        restantes.append(pedaco)

    if not restantes:
        return fim
    return f"{prefixo}{', '.join(restantes)}{fim}"


def excluir(ticker: str) -> dict[str, Any]:
    """Remove o ticker da lista oficial e corrige a contagem do cabeçalho."""

    normalizado = str(ticker or "").strip().upper()
    if not normalizado:
        raise ValueError("ticker inválido: informe um ticker")

    arquivo = caminho_lista()
    original = _ler_texto(arquivo)
    linhas, secoes = _analisar(original)

    alvo: dict[str, Any] | None = None
    for secao in secoes:
        classe = secao.get("classe")
        if classe is None or secao.get("linha_conteudo") is None:
            continue
        linha = linhas[int(secao["linha_conteudo"])]
        if normalizado in _tickers_da_linha(linha, classe):
            alvo = secao
            break

    if alvo is None:
        raise ValueError(f"{normalizado} não está na lista")

    classe = alvo["classe"]
    indice_conteudo = int(alvo["linha_conteudo"])
    linhas[indice_conteudo] = _remover_da_linha(
        linhas[indice_conteudo], classe, normalizado
    )
    contagem = len(_tickers_da_linha(linhas[indice_conteudo], classe))
    linhas[int(alvo["indice"])] = _atualizar_contagem(
        linhas[int(alvo["indice"])], contagem
    )
    _atualizar_linha_atualizado(linhas, "removido pela tela")
    _publicar(arquivo, original, "".join(linhas))

    return {
        "ticker": normalizado,
        "classe": classe,
        "secao": alvo["titulo"],
        "contagem": contagem,
        "acao": "excluido",
    }
