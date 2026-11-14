"""Geração, consulta e marcação de alertas.

Um alerta nasce de quatro situações: um ativo entrou numa lista, saiu de uma
lista, a última coleta de uma fonte falhou ou a cotação de um ativo está velha.
A chave ``(tipo, ticker, estrategia, data_ref)`` é única, então repetir a mesma
verificação não duplica registros.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import db
from app.motor import rankings
from app.web import formato

DIAS_UTEIS_LIMITE = 5
TIPOS = {
    "entrou": "Entrou na lista",
    "saiu": "Saiu da lista",
    "falha_coleta": "Falha de coleta",
    "desatualizado": "Cotação desatualizada",
}


def _agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _data_iso(valor: Any, padrao: date | None = None) -> str:
    if valor is not None:
        convertido = formato.em_brasilia(valor)
        if isinstance(convertido, datetime):
            return convertido.date().isoformat()
        texto = str(convertido).strip()[:10]
        try:
            return date.fromisoformat(texto).isoformat()
        except ValueError:
            pass
    return (padrao or date.today()).isoformat()


def criar(
    tipo: str,
    *,
    ticker: str | None = None,
    estrategia: str | None = None,
    titulo: str,
    motivo: str,
    data_ref: Any,
    criado_em: str | None = None,
) -> bool:
    """Insere um alerta; devolve ``True`` se ele ainda não existia."""

    with db.conectar() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO alertas (
                criado_em, tipo, ticker, estrategia, titulo, motivo, data_ref, lido
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                criado_em or _agora(),
                tipo,
                ticker,
                estrategia,
                titulo,
                motivo,
                _data_iso(data_ref),
            ),
        )
        return cursor.rowcount > 0


def _posicoes(conn: Any, ranking_id: int) -> dict[str, int]:
    linhas = conn.execute(
        """
        SELECT ticker, posicao
          FROM ranking_itens
         WHERE ranking_id = ? AND fora_da_lista = 0 AND posicao IS NOT NULL
        """,
        (ranking_id,),
    ).fetchall()
    return {linha["ticker"]: int(linha["posicao"]) for linha in linhas}


def gerar_alertas_rankings() -> dict[str, int]:
    """Compara cada ranking com o anterior e grava entradas e saídas."""

    inseridos = {"entrou": 0, "saiu": 0}
    with db.conectar() as conn:
        for estrategia in rankings.ORDEM:
            linhas = conn.execute(
                """
                SELECT id, gerado_em
                  FROM rankings
                 WHERE estrategia = ?
                 ORDER BY id DESC
                 LIMIT 2
                """,
                (estrategia,),
            ).fetchall()
            if len(linhas) < 2:
                continue

            atual = _posicoes(conn, int(linhas[0]["id"]))
            anterior = _posicoes(conn, int(linhas[1]["id"]))
            data_ref = _data_iso(linhas[0]["gerado_em"])
            referencia = date.fromisoformat(data_ref)
            nome = estrategia.replace("_", " ").title()

            for ticker in sorted(set(atual) - set(anterior)):
                criado = criar(
                    "entrou",
                    ticker=ticker,
                    estrategia=estrategia,
                    titulo=f"Entrou em {nome}",
                    motivo=(
                        f"estava fora do ranking anterior e entrou na lista de "
                        f"{nome}; referência: {formato.data_br(referencia)}"
                    ),
                    data_ref=data_ref,
                )
                inseridos["entrou"] += int(criado)

            for ticker in sorted(set(anterior) - set(atual)):
                criado = criar(
                    "saiu",
                    ticker=ticker,
                    estrategia=estrategia,
                    titulo=f"Saiu de {nome}",
                    motivo=(
                        f"estava na lista anterior e saiu de {nome}; "
                        f"referência: {formato.data_br(referencia)}"
                    ),
                    data_ref=data_ref,
                )
                inseridos["saiu"] += int(criado)
    return inseridos


def _ultima_coleta_bem_sucedida(conn: Any, fonte: str) -> str | None:
    instantes: list[str] = []
    for tabela in ("cotacoes", "fundamentos", "proventos"):
        valor = conn.execute(
            f"SELECT MAX(coletado_em) FROM {tabela} WHERE fonte = ?", (fonte,)
        ).fetchone()[0]
        if valor:
            instantes.append(str(valor))
    return max(instantes) if instantes else None


def gerar_alertas_falhas() -> int:
    """Grava alerta quando a última coleta de uma fonte terminou em falha."""

    inseridos = 0
    with db.conectar() as conn:
        fontes = [
            linha["fonte"]
            for linha in conn.execute(
                "SELECT DISTINCT fonte FROM falhas WHERE fonte IS NOT NULL ORDER BY fonte"
            ).fetchall()
        ]
        for fonte in fontes:
            ultima = conn.execute(
                """
                SELECT quando, ticker, etapa, erro
                  FROM falhas
                 WHERE fonte = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (fonte,),
            ).fetchone()
            if ultima is None or not ultima["quando"]:
                continue
            bem_sucedida = _ultima_coleta_bem_sucedida(conn, fonte)
            if bem_sucedida and str(ultima["quando"]) <= bem_sucedida:
                continue

            data_ref = _data_iso(ultima["quando"])
            detalhe = str(ultima["erro"] or "erro não informado")
            if ultima["etapa"]:
                detalhe = f"{ultima['etapa']}: {detalhe}"
            criado = criar(
                "falha_coleta",
                titulo=f"Falha na coleta de {fonte}",
                motivo=(
                    f"a última coleta da fonte {fonte} falhou em "
                    f"{formato.data_br(data_ref)}: {detalhe}"
                ),
                data_ref=data_ref,
            )
            inseridos += int(criado)
    return inseridos


def _dias_uteis(inicio: date, fim: date) -> int:
    total = 0
    atual = inicio
    while atual < fim:
        atual += timedelta(days=1)
        if atual.weekday() < 5:
            total += 1
    return total


def gerar_alertas_desatualizados(hoje: date | None = None) -> int:
    """Alerta para cada ativo cuja cotação mais recente tem mais de 5 dias úteis."""

    referencia = hoje or date.today()
    inseridos = 0
    cache: dict[str, int] = {}
    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT ticker, MAX(data) AS ultima FROM cotacoes GROUP BY ticker"
        ).fetchall()

    for linha in linhas:
        ticker = str(linha["ticker"])
        ultima_texto = _data_iso(linha["ultima"])
        ultima = date.fromisoformat(ultima_texto)
        if ultima_texto not in cache:
            cache[ultima_texto] = _dias_uteis(ultima, referencia)
        dias = cache[ultima_texto]
        if dias <= DIAS_UTEIS_LIMITE:
            continue
        criado = criar(
            "desatualizado",
            ticker=ticker,
            titulo=f"Cotação desatualizada: {ticker}",
            motivo=(
                f"última cotação em {formato.data_br(ultima)}, há {dias} dias "
                f"úteis; critério: mais de {DIAS_UTEIS_LIMITE} dias úteis sem barra "
                f"nova (referência: {formato.data_br(referencia)})"
            ),
            data_ref=ultima_texto,
        )
        inseridos += int(criado)
    return inseridos


def listar(lidos: int | None = None, limite: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alertas"
    parametros: list[Any] = []
    if lidos is not None:
        sql += " WHERE lido = ?"
        parametros.append(int(bool(lidos)))
    sql += " ORDER BY id DESC"
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(int(limite))
    with db.conectar() as conn:
        linhas = conn.execute(sql, parametros).fetchall()
    return [{chave: linha[chave] for chave in linha.keys()} for linha in linhas]


def marcar_lido(id: int) -> bool:
    with db.conectar() as conn:
        cursor = conn.execute("UPDATE alertas SET lido = 1 WHERE id = ?", (id,))
        return cursor.rowcount > 0


def contar_nao_lidos() -> int:
    with db.conectar() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM alertas WHERE lido = 0").fetchone()[0]
        )
