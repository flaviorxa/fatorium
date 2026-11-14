"""Coleta de cotações, proventos e fundamentos.

Uso:
    python -m scripts.coletar --tickers PETR4,VALE3 --periodo 2y
"""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any

from app import config, db
from app.fontes import (
    btg,
    cripto,
    cvm,
    fundamentus_br,
    fundsexplorer,
    genial,
    msn,
    statusinvest,
    yahoo,
)
from app.fontes.base import Resultado

logger = logging.getLogger(__name__)

_CLASSES_BR = {"acao_b3", "fii"}
_CLASSES_YAHOO_INFO = {"acao_b3", "fii", "bdr"}

_CAMPOS_YAHOO = {
    "price_to_book": "pvp_yahoo",
    "book_value": "vpa_yahoo",
    "market_cap": "market_cap_yahoo",
    "nome_longo": "nome_longo_yahoo",
    "moeda": "moeda_yahoo",
}


def _agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ler_mapa_fii() -> dict[str, dict[str, Any]]:
    return config.ler_mapa_fii()


def _campos_cvm(fundo: dict[str, Any]) -> dict[str, Any]:
    taxa = fundo.get("taxa_administracao_informe")
    return {
        "vpa_cvm": fundo.get("vpa"),
        "patrimonio_liquido_cvm": fundo.get("patrimonio_liquido"),
        "cotas_emitidas": fundo.get("cotas"),
        "cotistas": fundo.get("cotistas"),
        "taxa_administracao_informe": taxa,
        "taxa_administracao_campo": fundo.get(
            "taxa_administracao_campo", cvm.CAMPO_TAXA_ADMINISTRACAO
        ),
        "segmento_cvm": fundo.get("segmento"),
    }


def _coletar_um(
    ticker: str,
    classe: str | None,
    periodo: str,
    fundos_cvm: dict[str, dict[str, Any]] | None = None,
    mapa_fii: dict[str, dict[str, Any]] | None = None,
    somente_fundamentos: bool = False,
    fundos_alt: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    # Tudo no site é exibido em reais, inclusive cripto (par BTC-BRL).
    moeda = "BRL"
    fonte_cotacao = "okx" if classe == "cripto" else "yahoo"
    db.upsert_ativo(ticker, classe=classe, moeda=moeda, fonte=fonte_cotacao)

    avisos: list[str] = []
    barras = 0
    proventos = 0
    fundamentos = 0
    status = "falha"
    erros: list[str] = []

    if not somente_fundamentos:
        # Cripto precisa de par em reais: o Yahoo só tem BTC-USD e o site
        # imprime R$, o que mostrava o Bitcoin ~5x abaixo do preço real.
        historico = (
            cripto.historico(ticker, periodo)
            if classe == "cripto"
            else yahoo.historico(ticker, periodo)
        )
        if isinstance(historico, Resultado):
            erros.append(f"cotações: {historico.erro}")
        else:
            barras = db.gravar_cotacoes(ticker, historico, fonte_cotacao)
            avisos.extend(historico.attrs.get("avisos", []))

        if classe != "cripto":  # cripto não paga provento
            proventos_df = yahoo.proventos(ticker)
            if isinstance(proventos_df, Resultado):
                erros.append(f"proventos: {proventos_df.erro}")
            else:
                proventos = db.gravar_proventos(ticker, proventos_df, "yahoo")

    # Cada classe tem sua fonte: ação no Fundamentus, FII no Funds Explorer
    # (o Fundamentus erra o DY de FII e cobre 23/34) e ETF no Status Invest
    # (patrimônio, cotistas e taxa de administração, que a CVM não publica).
    fontes_por_classe = {
        "fii": ("fundsexplorer", fundsexplorer),
        "etf": ("statusinvest", statusinvest),
    }
    if classe in _CLASSES_BR or classe in fontes_por_classe:
        fonte_nome, fonte = fontes_por_classe.get(
            classe or "", ("fundamentus", fundamentus_br)
        )
        try:
            campos, avisos_fund = fonte.papel(ticker, classe)
        except ValueError as exc:
            campos, avisos_fund = {}, [str(exc)]
        except Exception as exc:  # noqa: BLE001
            campos, avisos_fund = {}, [f"{type(exc).__name__}: {exc}"]

        avisos.extend(avisos_fund)
        if campos:
            data_ref = campos.get("data_cotacao") or date.today().isoformat()
            fundamentos = db.gravar_fundos(ticker, data_ref, campos, fonte_nome)
            nome = campos.get("empresa")
            setor = campos.get("setor")
            db.upsert_ativo(
                ticker,
                nome=str(nome) if nome else None,
                classe=classe,
                setor=str(setor) if setor else None,
                moeda=moeda,
                fonte=fonte_nome,
            )
        else:
            erros.append("fundamentos: sem dados")
            db.registrar_falha(
                fonte_nome,
                ticker,
                "fundamentos",
                "; ".join(avisos_fund) or "sem dados",
            )

    nome_yahoo: str | None = None
    if classe in _CLASSES_YAHOO_INFO:
        basicos = yahoo.fundamentos_basicos(ticker)
        nome_yahoo = basicos.get("nome_longo")
        campos_yahoo = {
            destino: basicos.get(origem)
            for origem, destino in _CAMPOS_YAHOO.items()
        }
        if any(valor is not None for valor in campos_yahoo.values()):
            fundamentos += db.gravar_fundos(
                ticker, date.today().isoformat(), campos_yahoo, "yahoo"
            )

    if classe == "fii":
        entrada = (mapa_fii or {}).get(ticker)
        if entrada is None and fundos_cvm and nome_yahoo:
            # FII novo no universo: o mapa é fixo, então casa pelo nome do
            # yfinance com o nome publicado pela CVM (mesma regra do script).
            cnpj_casado = cvm.casar_por_nome(nome_yahoo, fundos_cvm)
            if cnpj_casado:
                entrada = {
                    "cnpj": cnpj_casado,
                    "nome_cvm": fundos_cvm[cnpj_casado].get("nome"),
                }
                avisos.append(
                    f"CVM: CNPJ {cnpj_casado} casado pelo nome do fundo"
                )
        if entrada is None:
            avisos.append(
                "CVM: ticker sem mapa de CNPJ; Fi-Infra/FIP pode não ter valor de cota"
            )
            db.registrar_falha(
                "cvm", ticker, "mapeamento", "ticker sem CNPJ mapeado"
            )
        else:
            cnpj = str(entrada.get("cnpj"))
            fundo = (fundos_cvm or {}).get(cnpj) or (fundos_alt or {}).get(cnpj)
            if fundo is None:
                motivo = "FII não está nos informes da CVM (mensal, diário, Fiagro, FIP)"
                avisos.append(f"CVM: {motivo}")
                db.registrar_falha("cvm", ticker, "informe_mensal", motivo)
            elif fundo.get("data_ref"):
                fundamentos_cvm = db.gravar_fundos(
                    ticker,
                    fundo["data_ref"],
                    _campos_cvm(fundo),
                    "cvm",
                )
                fundamentos += fundamentos_cvm
                segmento = fundo.get("segmento")
                if segmento:
                    with db.conectar() as conn:
                        conn.execute(
                            "UPDATE ativos SET setor = ? "
                            "WHERE ticker = ? "
                            "AND (setor IS NULL OR TRIM(setor) = '')",
                            (segmento, ticker),
                        )

    # Índice e cripto não têm fundamento em fonte aberta; o valor de mercado
    # já vem das cotações. Registra o último fechamento e deixa de contar
    # falha na coleta de fundamentos.
    if classe in {"indice", "cripto"}:
        linhas = db.consultar(
            "SELECT fechamento, data FROM cotacoes WHERE ticker = ? "
            "ORDER BY data DESC LIMIT 2",
            (ticker,),
        )
        if linhas and linhas[0]["fechamento"] is not None:
            campos_mercado = {"preco": linhas[0]["fechamento"]}
            anterior = linhas[1]["fechamento"] if len(linhas) > 1 else None
            if anterior:
                campos_mercado["variacao_pct"] = (
                    linhas[0]["fechamento"] / anterior - 1
                ) * 100
            fundamentos += db.gravar_fundos(
                ticker, str(linhas[0]["data"]), campos_mercado, fonte_cotacao
            )
        else:
            avisos.append("sem cotação para registrar o valor de mercado")

    if somente_fundamentos:
        if fundamentos > 0:
            status = "ok" if not erros else "parcial"
    elif barras > 0:
        status = "ok" if not erros else "parcial"
    elif proventos > 0 or fundamentos > 0:
        status = "parcial"

    return {
        "ticker": ticker,
        "classe": classe or "-",
        "barras": barras,
        "proventos": proventos,
        "fundamentos": fundamentos,
        "status": status,
        "avisos": avisos,
        "erros": erros,
    }


def coletar_consenso(tickers: list[str]) -> dict[str, Any]:
    """Coleta apenas o consenso de analistas do MSN, em paralelo (3 workers)."""

    iniciado = _agora()
    lista = [t.strip().upper() for t in tickers if t and t.strip()]
    resultados: dict[str, dict[str, Any]] = {}

    if lista:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futuros = {
                executor.submit(msn.consenso, ticker): ticker for ticker in lista
            }
            for futuro in as_completed(futuros):
                ticker = futuros[futuro]
                try:
                    campos, avisos = futuro.result()
                except Exception as exc:  # noqa: BLE001
                    motivo = f"{type(exc).__name__}: {exc}"
                    db.registrar_falha("msn", ticker, "consenso", motivo)
                    campos, avisos = {}, [motivo]

                data_ref = campos.pop("data_ref", None)
                gravados = 0
                if campos and data_ref:
                    gravados = db.gravar_fundos(ticker, data_ref, campos, "msn")
                erro = any(
                    aviso == "ticker sem página no MSN"
                    or aviso.startswith("falha ao consultar MSN:")
                    for aviso in avisos
                )
                resultados[ticker] = {
                    "ticker": ticker,
                    "gravados": gravados,
                    "campos": campos,
                    "data_ref": data_ref,
                    "avisos": avisos,
                    "status": (
                        "ok"
                        if gravados
                        else ("falha" if erro else "sem_consenso")
                    ),
                }

    ordenados = [resultados[t] for t in sorted(resultados)]
    ok = sum(1 for item in ordenados if item["status"] == "ok")
    sem_consenso = sum(1 for item in ordenados if item["status"] == "sem_consenso")
    falhas = sum(1 for item in ordenados if item["status"] == "falha")

    print("ticker  campos  data_ref    status")
    for item in ordenados:
        print(
            f"{item['ticker']:<7} {item['gravados']:>6}  "
            f"{item['data_ref'] or '-':<10}  {item['status']}"
        )
        for aviso in item.get("avisos", []):
            print(f"  ! {aviso}")
    print(f"total ok={ok} sem_consenso={sem_consenso} falhas={falhas}")

    return {
        "iniciado_em": iniciado,
        "terminado_em": _agora(),
        "total": len(ordenados),
        "ok": ok,
        "falhas": falhas,
        "sem_consenso": sem_consenso,
        "por_ticker": {item["ticker"]: item for item in ordenados},
    }


def _coletar_precos_alvo(
    fonte: Any,
    nome: str,
    campo_alvo: str,
    tickers: list[str],
    *,
    workers: int = 3,
) -> dict[str, Any]:
    """Coleta preço-alvo de uma fonte por ativo, em paralelo.

    Só ação da B3 entra: Genial devolve 422 e a página do BTG não é usada para
    FII/ETF hoje. Ticker fora do universo é tentado (inclusão manual).
    """

    iniciado = _agora()
    try:
        universo = config.ler_universo()
    except FileNotFoundError:
        universo = {}
    lista = [
        t.strip().upper()
        for t in tickers
        if t and t.strip() and universo.get(t.strip().upper(), "acao_b3") == "acao_b3"
    ]
    resultados: dict[str, dict[str, Any]] = {}

    if lista:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futuros = {
                executor.submit(fonte.preco_alvo, ticker): ticker for ticker in lista
            }
            for futuro in as_completed(futuros):
                ticker = futuros[futuro]
                try:
                    campos, avisos = futuro.result()
                except Exception as exc:  # noqa: BLE001
                    motivo = f"{type(exc).__name__}: {exc}"
                    db.registrar_falha(nome, ticker, "preco_alvo", motivo)
                    campos, avisos = {}, [motivo]

                gravados = 0
                if campos:
                    gravados = db.gravar_fundos(
                        ticker, date.today().isoformat(), campos, nome
                    )
                erro = any(
                    aviso.startswith("falha ao consultar") for aviso in avisos
                )
                resultados[ticker] = {
                    "ticker": ticker,
                    "gravados": gravados,
                    "campos": campos,
                    "avisos": avisos,
                    "status": (
                        "ok" if gravados else ("falha" if erro else "sem_cobertura")
                    ),
                }

    ordenados = [resultados[t] for t in sorted(resultados)]
    ok = sum(1 for item in ordenados if item["status"] == "ok")
    falhas = sum(1 for item in ordenados if item["status"] == "falha")
    sem = sum(1 for item in ordenados if item["status"] == "sem_cobertura")

    print("ticker  alvo     status")
    for item in ordenados:
        alvo = item["campos"].get(campo_alvo)
        print(
            f"{item['ticker']:<7} {alvo if alvo is not None else '-':<8} "
            f"{item['status']}"
        )
        for aviso in item.get("avisos", []):
            print(f"  ! {aviso}")
    print(f"total ok={ok} sem_cobertura={sem} falhas={falhas}")

    return {
        "iniciado_em": iniciado,
        "terminado_em": _agora(),
        "total": len(ordenados),
        "ok": ok,
        "sem_cobertura": sem,
        "falhas": falhas,
        "por_ticker": {item["ticker"]: item for item in ordenados},
    }


def coletar_genial(tickers: list[str]) -> dict[str, Any]:
    """Preço-alvo do Genial Analisa (3 workers; o leitor público é lento)."""

    return _coletar_precos_alvo(genial, "genial", "genial_alvo", tickers)


def coletar_btg(tickers: list[str]) -> dict[str, Any]:
    """Preço-alvo do Research BTG Pactual (4 workers; SSR rápido)."""

    return _coletar_precos_alvo(
        btg, "btg", "btg_alvo", tickers, workers=4
    )


def coletar(
    tickers: list[str],
    periodo: str = config.PERIODO_PADRAO,
    *,
    somente_fundamentos: bool = False,
) -> dict[str, Any]:
    """Coleta em paralelo e devolve o resumo (também usado pela API).

    Com ``somente_fundamentos=True`` o histórico de cotações e os proventos não
    são consultados: o job de fundamentos atualiza apenas Fundamentus, CVM e os
    dados básicos do Yahoo.
    """

    iniciado = _agora()
    lista = [t.strip().upper() for t in tickers if t and t.strip()]
    removidas = db.limpar_falhas_mapeamento_resolvidas()
    if removidas:
        logger.info(
            "falhas de mapeamento já resolvidas removidas: %d", removidas
        )

    try:
        universo = config.ler_universo()
    except FileNotFoundError:
        if not lista:
            raise
        universo = {}

    mapa_fii = _ler_mapa_fii()
    tem_fii = any(universo.get(ticker) == "fii" for ticker in lista)
    if tem_fii and len(mapa_fii) < 15:
        logger.warning(
            "mapa ticker-CNPJ de FII tem %d entradas; o mapeamento precisa "
            "de revisão manual",
            len(mapa_fii),
        )

    fundos_cvm: dict[str, dict[str, Any]] = {}
    fundos_alt: dict[str, dict[str, Any]] = {}
    if mapa_fii and tem_fii:
        ano = date.today().year
        try:
            diretorio = cvm.baixar_mensal(ano)
            fundos_cvm = cvm.ler_fundos(ano, diretorio)
        except Exception as exc:  # noqa: BLE001 - a coleta continua sem a CVM
            db.registrar_falha(
                "cvm", None, "mensal", f"{type(exc).__name__}: {exc}"
            )

        # Fi-Infra, Fiagro e FIP-Infra ficam fora do informe mensal de FII;
        # o valor patrimonial da cota está no diário de FI, no mensal de
        # Fiagro e no quadrimestral de FIP.
        faltando = {
            str(entrada["cnpj"])
            for ticker, entrada in mapa_fii.items()
            if ticker in lista and entrada.get("cnpj")
        } - set(fundos_cvm)
        if faltando:
            try:
                fundos_alt = cvm.ler_fundos_alternativos(ano, faltando)
            except Exception as exc:  # noqa: BLE001 - segue sem os alternativos
                db.registrar_falha(
                    "cvm", None, "alternativos", f"{type(exc).__name__}: {exc}"
                )

    resultados: dict[str, dict[str, Any]] = {}
    if lista:
        with ThreadPoolExecutor(max_workers=config.MAX_CONCORRENCIA) as executor:
            futuros = {
                executor.submit(
                    _coletar_um,
                    ticker,
                    universo.get(ticker),
                    periodo,
                    fundos_cvm,
                    mapa_fii,
                    somente_fundamentos,
                    fundos_alt,
                ): ticker
                for ticker in lista
            }
            for futuro in as_completed(futuros):
                ticker = futuros[futuro]
                try:
                    resultados[ticker] = futuro.result()
                except Exception as exc:  # noqa: BLE001
                    db.registrar_falha("sistema", ticker, "coleta", f"{type(exc).__name__}: {exc}")
                    resultados[ticker] = {
                        "ticker": ticker,
                        "classe": universo.get(ticker, "-"),
                        "barras": 0,
                        "proventos": 0,
                        "fundamentos": 0,
                        "status": "falha",
                        "avisos": [],
                        "erros": [str(exc)],
                    }

    ordenados = [resultados[t] for t in sorted(resultados)]
    ok = sum(1 for item in ordenados if item["status"] == "ok")
    falhas = len(ordenados) - ok

    print("ticker  classe  barras  proventos  fundamentos  status")
    for item in ordenados:
        print(
            f"{item['ticker']:<7} {item['classe']:<7} {item['barras']:>6} "
            f"{item['proventos']:>10} {item['fundamentos']:>12}  {item['status']}"
        )
        for erro in item.get("erros", []):
            print(f"  ! {erro}")
    print(f"total ok={ok} falhas={falhas}")

    return {
        "iniciado_em": iniciado,
        "terminado_em": _agora(),
        "periodo": periodo,
        "total": len(ordenados),
        "ok": ok,
        "falhas": falhas,
        "falhas_mapeamento_removidas": removidas,
        "por_ticker": {item["ticker"]: item for item in ordenados},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta cotações, proventos e fundamentos.")
    parser.add_argument("--tickers", help="lista separada por vírgulas; padrão: universo do markdown")
    parser.add_argument("--periodo", default=config.PERIODO_PADRAO)
    parser.add_argument(
        "--consenso",
        action="store_true",
        help="coleta apenas o consenso de analistas do MSN",
    )
    parser.add_argument(
        "--genial",
        action="store_true",
        help="coleta apenas o preço-alvo do Genial Analisa",
    )
    parser.add_argument(
        "--btg",
        action="store_true",
        help="coleta apenas o preço-alvo do Research BTG Pactual",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = list(config.ler_universo().keys())

    if args.btg:
        tipo = "btg"
    elif args.genial:
        tipo = "genial"
    elif args.consenso:
        tipo = "consenso"
    else:
        tipo = "cli"
    id_coleta = db.abrir_coleta_ou_recusar(tipo)
    if id_coleta is None:
        print("coleta já em andamento")
        return
    try:
        if args.consenso:
            resumo = coletar_consenso(tickers)
        elif args.genial:
            resumo = coletar_genial(tickers)
        elif args.btg:
            resumo = coletar_btg(tickers)
        else:
            resumo = coletar(tickers, args.periodo)
    except Exception as exc:  # noqa: BLE001 - registra e propaga
        db.finalizar_coleta(id_coleta, "falha", {"erro": f"{type(exc).__name__}: {exc}"})
        raise
    status = "ok" if resumo.get("falhas", 0) == 0 else "parcial"
    db.finalizar_coleta(id_coleta, status, resumo)


if __name__ == "__main__":
    main()
