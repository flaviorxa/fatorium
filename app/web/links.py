"""Links do ativo para os sites de referência.

Um lugar só decide o caminho por classe: os sites usam pastas diferentes
para ação, BDR, FII e ETF (e o Investidor10 devolve 410 quando o BDR entra
em ``/acoes/``).
"""

from __future__ import annotations

INVESTIDOR10 = "https://investidor10.com.br"
STATUSINVEST = "https://statusinvest.com.br"
FUNDSEXPLORER = "https://www.fundsexplorer.com.br"
GENIAL = "https://analisa.genialinvestimentos.com.br"
BTG = "https://content.btgpactual.com/research/ativo"


def links_externos(
    ticker: str, classe: str | None, msn_url: str | None = None
) -> list[dict[str, str]]:
    """Lista ordenada de ``{"nome", "url"}`` que fazem sentido para a classe."""

    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return []
    classe = str(classe or "").strip().lower()
    links: list[dict[str, str]] = []

    def adicionar(nome: str, url: str) -> None:
        links.append({"nome": nome, "url": url})

    if msn_url:
        adicionar("MSN Dinheiro", msn_url)

    if classe in {"acao_b3", "bdr"}:
        pasta = "acoes" if classe == "acao_b3" else "bdrs"
        adicionar("Investidor10", f"{INVESTIDOR10}/{pasta}/{ticker}/")
        adicionar("Status Invest", f"{STATUSINVEST}/acoes/{ticker.lower()}")
        adicionar("Genial Analisa", f"{GENIAL}/acoes/{ticker}/")
        adicionar("BTG Research", f"{BTG}/{ticker}")
    elif classe == "fii":
        adicionar("Investidor10", f"{INVESTIDOR10}/fiis/{ticker}/")
        adicionar("Funds Explorer", f"{FUNDSEXPLORER}/funds/{ticker}")
        adicionar(
            "Status Invest", f"{STATUSINVEST}/fundos-imobiliarios/{ticker.lower()}"
        )
    elif classe == "etf":
        adicionar("Investidor10", f"{INVESTIDOR10}/etfs/{ticker}/")
        adicionar("Status Invest", f"{STATUSINVEST}/etfs/{ticker.lower()}")

    return links
