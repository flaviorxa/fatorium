"""Acesso ao SQLite da plataforma.

Sem ORM: apenas a biblioteca padrão. Todas as datas são gravadas como texto
``YYYY-MM-DD`` e todas as escritas são idempotentes.

O ciclo de vida de uma coleta usa um único vocabulário de status, definido em
``STATUS_COLETA``: ``em_andamento``, ``ok``, ``parcial``, ``falha`` e
``interrompida``. Não existe outro valor para ``coletas.status``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from app import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

STATUS_COLETA = (
    "em_andamento",
    "ok",
    "parcial",
    "falha",
    "interrompida",
)


def _agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_iso(valor: Any) -> str | None:
    """Normaliza uma data para ``YYYY-MM-DD``; devolve ``None`` se inválida."""

    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, pd.Timestamp):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")

    texto = str(valor).strip()
    if not texto:
        return None

    texto = texto[:10]
    try:
        if "/" in texto:
            return datetime.strptime(texto, "%d/%m/%Y").strftime("%Y-%m-%d")
        return date.fromisoformat(texto).isoformat()
    except ValueError:
        return None


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


def _aplicar_migracoes(conn: sqlite3.Connection) -> None:
    """Migrações idempotentes para bancos criados por versões anteriores."""

    colunas = {
        linha["name"] for linha in conn.execute("PRAGMA table_info(ranking_itens)")
    }
    if "fora_da_lista" not in colunas:
        conn.execute(
            "ALTER TABLE ranking_itens "
            "ADD COLUMN fora_da_lista INTEGER NOT NULL DEFAULT 0"
        )

    colunas_ativos = {
        linha["name"] for linha in conn.execute("PRAGMA table_info(ativos)")
    }
    if "ativo" not in colunas_ativos:
        conn.execute(
            "ALTER TABLE ativos ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1"
        )

    colunas_config = {
        linha["name"] for linha in conn.execute("PRAGMA table_info(configuracoes)")
    }
    if "valor" not in colunas_config:
        conn.execute("ALTER TABLE configuracoes ADD COLUMN valor TEXT")


def _aplicar(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _aplicar_migracoes(conn)


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    """Abre a conexão já configurada e com o schema aplicado."""

    caminho = Path(config.DB_PATH)
    caminho.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(caminho, timeout=config.TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _aplicar(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def aplicar_schema() -> None:
    """Cria as tabelas de forma idempotente."""

    with conectar():
        pass


def upsert_ativo(
    ticker: str,
    nome: str | None = None,
    classe: str | None = None,
    setor: str | None = None,
    moeda: str | None = None,
    fonte: str | None = None,
) -> None:
    with conectar() as conn:
        conn.execute(
            """
            INSERT INTO ativos (ticker, nome, classe, setor, moeda, fonte, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                nome = COALESCE(excluded.nome, ativos.nome),
                classe = COALESCE(excluded.classe, ativos.classe),
                setor = COALESCE(excluded.setor, ativos.setor),
                moeda = COALESCE(excluded.moeda, ativos.moeda),
                fonte = COALESCE(excluded.fonte, ativos.fonte),
                atualizado_em = excluded.atualizado_em
            """,
            (ticker, nome, classe, setor, moeda, fonte, _agora()),
        )


def marcar_ativo(ticker: str, ativo: bool) -> None:
    """Marca (1) ou desmarca (0) o ativo, sem tocar no histórico de cotações."""

    ticker = str(ticker or "").strip().upper()
    with conectar() as conn:
        conn.execute(
            "UPDATE ativos SET ativo = ? WHERE ticker = ?",
            (1 if ativo else 0, ticker),
        )


def ativos_com_status() -> list[dict[str, Any]]:
    """Ativos do banco com a marca operacional e a última cotação conhecida."""

    with conectar() as conn:
        linhas = conn.execute(
            """
            SELECT a.ticker, a.nome, a.classe, a.setor, a.moeda,
                   a.ativo, c.data AS ultima_data,
                   c.fechamento AS ultimo_fechamento
              FROM ativos a
              LEFT JOIN cotacoes c
                ON c.ticker = a.ticker
               AND c.data = (
                   SELECT MAX(x.data) FROM cotacoes x WHERE x.ticker = a.ticker
               )
             ORDER BY a.ticker
            """
        ).fetchall()
    return [dict(linha) for linha in linhas]


def gravar_cotacoes(ticker: str, df: pd.DataFrame, fonte: str = "yahoo") -> int:
    """Grava/atualiza cotações. ``df`` deve ter as colunas de ``app.fontes.base``."""

    if df is None or len(df) == 0:
        return 0

    agora = _agora()
    linhas: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        data = data_iso(row.get("data"))
        fechamento = _numero(row.get("fechamento"))
        if data is None or fechamento is None:
            continue
        linhas.append(
            (
                ticker,
                data,
                _numero(row.get("abertura")),
                _numero(row.get("maxima")),
                _numero(row.get("minima")),
                fechamento,
                _numero(row.get("fechamento_ajustado")),
                _numero(row.get("volume")),
                fonte,
                agora,
            )
        )

    if not linhas:
        return 0

    with conectar() as conn:
        conn.executemany(
            """
            INSERT INTO cotacoes (
                ticker, data, abertura, maxima, minima, fechamento,
                fechamento_ajustado, volume, fonte, coletado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, data) DO UPDATE SET
                abertura = excluded.abertura,
                maxima = excluded.maxima,
                minima = excluded.minima,
                fechamento = excluded.fechamento,
                fechamento_ajustado = excluded.fechamento_ajustado,
                volume = excluded.volume,
                fonte = excluded.fonte,
                coletado_em = excluded.coletado_em
            """,
            linhas,
        )
    return len(linhas)


def gravar_proventos(ticker: str, df: pd.DataFrame, fonte: str = "yahoo") -> int:
    if df is None or len(df) == 0:
        return 0

    agora = _agora()
    linhas: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        data_ex = data_iso(row.get("data_ex"))
        valor = _numero(row.get("valor"))
        if data_ex is None or valor is None or valor <= 0:
            continue
        tipo = str(row.get("tipo") or "dividendo")
        linhas.append((ticker, data_ex, valor, tipo, fonte, agora))

    if not linhas:
        return 0

    with conectar() as conn:
        conn.executemany(
            """
            INSERT INTO proventos (ticker, data_ex, valor, tipo, fonte, coletado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, data_ex, tipo, valor) DO UPDATE SET
                fonte = excluded.fonte,
                coletado_em = excluded.coletado_em
            """,
            linhas,
        )
    return len(linhas)


def _valor_fundamento(valor: Any) -> float | str | None:
    """Preserva texto (ex.: segmento da CVM) e converte o resto em número."""

    if valor is None or isinstance(valor, bool):
        return None
    numero = _numero(valor)
    if numero is not None:
        return numero
    if isinstance(valor, str):
        texto = valor.strip()
        return texto or None
    return None


def gravar_fundos(
    ticker: str,
    data_ref: str,
    campos: dict[str, Any],
    fonte: str = "fundamentus",
) -> int:
    """Grava fundamentos em formato longo, ignorando campos sem valor."""

    referencia = data_iso(data_ref)
    if referencia is None or not campos:
        return 0

    agora = _agora()
    linhas: list[tuple[Any, ...]] = []
    for campo, valor in campos.items():
        gravavel = _valor_fundamento(valor)
        if gravavel is None:
            continue
        linhas.append((ticker, referencia, str(campo), gravavel, fonte, agora))

    if not linhas:
        return 0

    with conectar() as conn:
        conn.executemany(
            """
            INSERT INTO fundamentos (ticker, data_ref, campo, valor, fonte, coletado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, data_ref, campo) DO UPDATE SET
                valor = excluded.valor,
                fonte = excluded.fonte,
                coletado_em = excluded.coletado_em
            """,
            linhas,
        )
    return len(linhas)


PRIORIDADE_FONTES = {"fundamentus": 4, "cvm": 3, "yahoo": 2, "msn": 1}


def fundamentos_mais_recentes(ticker: str) -> dict[str, dict[str, Any]]:
    """Devolve o valor mais recente de **cada campo** de fundamentos.

    A janela é por campo, não global: se o Fundamentus gravou ``pl`` em
    2026-10-27 e o Yahoo gravou ``preco`` em 2026-10-29, os dois aparecem, cada
    um com a sua ``data_ref`` e a sua ``fonte``. O retorno é
    ``{campo: {"valor", "fonte", "data_ref"}}``.

    Quando o mesmo campo existe em mais de uma fonte na mesma ``data_ref``,
    vence a prioridade ``fundamentus`` > ``cvm`` > ``yahoo`` > ``msn`` (fontes
    desconhecidas ficam por último).
    """

    ticker = str(ticker).strip().upper()
    with conectar() as conn:
        linhas = conn.execute(
            """
            SELECT campo, valor, fonte, data_ref
              FROM fundamentos
             WHERE ticker = ?
            """,
            (ticker,),
        ).fetchall()

    resultado: dict[str, dict[str, Any]] = {}
    escolhido: dict[str, tuple[str, int]] = {}
    for linha in linhas:
        campo = linha["campo"]
        prioridade = PRIORIDADE_FONTES.get(str(linha["fonte"] or ""), 0)
        chave = (str(linha["data_ref"] or ""), prioridade)
        if campo not in escolhido or chave > escolhido[campo]:
            escolhido[campo] = chave
            resultado[campo] = {
                "valor": linha["valor"],
                "fonte": linha["fonte"],
                "data_ref": linha["data_ref"],
            }
    return resultado


def registrar_falha(
    fonte: str,
    ticker: str | None = None,
    etapa: str | None = None,
    erro: str | None = None,
) -> None:
    with conectar() as conn:
        conn.execute(
            "INSERT INTO falhas (quando, fonte, ticker, etapa, erro) VALUES (?, ?, ?, ?, ?)",
            (_agora(), fonte, ticker, etapa, (erro or "")[:2000]),
        )


def limpar_falhas_mapeamento_resolvidas() -> int:
    """Apaga falhas de mapeamento de FIIs que já têm CNPJ no mapa.

    Só remove linhas ``fonte='cvm' AND etapa='mapeamento'`` cujo ticker está
    entre as chaves atuais de ``config/mapa_fii_cnpj.json``. Tickers que
    continuam fora do mapa permanecem. A operação é idempotente.
    """

    mapeados = sorted(str(ticker) for ticker in config.ler_mapa_fii())
    if not mapeados:
        return 0
    marcadores = ", ".join("?" for _ in mapeados)
    with conectar() as conn:
        cursor = conn.execute(
            f"""
            DELETE FROM falhas
             WHERE fonte = 'cvm'
               AND etapa = 'mapeamento'
               AND ticker IN ({marcadores})
            """,
            mapeados,
        )
        return int(cursor.rowcount)


def iniciar_coleta(tipo: str = "completa") -> int:
    with conectar() as conn:
        cur = conn.execute(
            "INSERT INTO coletas (iniciado_em, tipo, status) VALUES (?, ?, ?)",
            (_agora(), tipo, "em_andamento"),
        )
        return int(cur.lastrowid)


def _para_utc(valor: datetime | str) -> datetime:
    """Normaliza um instante para UTC (datas sem fuso são tratadas como UTC)."""

    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(timezone.utc)


def abrir_coleta_ou_recusar(
    tipo: str,
    agora: datetime | None = None,
    limite_minutos: int = 30,
) -> int | None:
    """Abre uma coleta ou recusa se já existe outra em andamento há pouco tempo.

    A verificação e a inserção acontecem na mesma transação ``BEGIN IMMEDIATE``:
    dois processos simultâneos não conseguem enxergar "não existe coleta" ao
    mesmo tempo. Uma coleta ``em_andamento`` iniciada há mais de
    ``limite_minutos`` é marcada como ``interrompida`` e libera a trava; uma
    coleta recente faz a chamada devolver ``None``.

    ``agora`` é injetável para os testes.
    """

    instante = _para_utc(agora or datetime.now(timezone.utc))
    referencia = instante.replace(microsecond=0).isoformat()
    limite = (instante - timedelta(minutes=limite_minutos)).replace(
        microsecond=0
    ).isoformat()

    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE coletas
               SET status = 'interrompida',
                   terminado_em = COALESCE(terminado_em, ?)
             WHERE status = 'em_andamento'
               AND iniciado_em < ?
            """,
            (referencia, limite),
        )
        cursor = conn.execute(
            """
            INSERT INTO coletas (iniciado_em, tipo, status)
            SELECT ?, ?, 'em_andamento'
             WHERE NOT EXISTS (
                 SELECT 1 FROM coletas WHERE status = 'em_andamento'
             )
            """,
            (referencia, tipo),
        )
        if cursor.rowcount == 0:
            return None
        return int(cursor.lastrowid)


def encerrar_coletas_orfas(agora: datetime | None = None) -> int:
    """Encerra coletas ``em_andamento`` deixadas por processos que morreram.

    Marca cada uma como ``interrompida`` com ``terminado_em`` preenchido e grava
    uma linha em ``falhas`` (``fonte='sistema'``). É idempotente: uma segunda
    chamada não encontra mais coletas em andamento e não duplica falhas.
    """

    instante = _para_utc(agora or datetime.now(timezone.utc)).replace(
        microsecond=0
    ).isoformat()

    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [
            int(linha["id"])
            for linha in conn.execute(
                "SELECT id FROM coletas WHERE status = 'em_andamento'"
            ).fetchall()
        ]
        if not ids:
            return 0
        conn.executemany(
            "INSERT INTO falhas (quando, fonte, etapa, erro) VALUES (?, ?, ?, ?)",
            [
                (
                    instante,
                    "sistema",
                    "coleta",
                    "processo encerrado durante a coleta",
                )
                for _ in ids
            ],
        )
        conn.executemany(
            """
            UPDATE coletas
               SET status = 'interrompida',
                   terminado_em = COALESCE(terminado_em, ?)
             WHERE id = ?
            """,
            [(instante, id_coleta) for id_coleta in ids],
        )
        return len(ids)


def podar_rankings(dias: int = 30) -> int:
    """Apaga gerações de ranking mais antigas que ``dias``.

    Cada execução grava 12 listas com todos os itens em JSON (~700 KB); sem
    poda o banco cresce ~2 MB por dia. A geração mais recente de cada
    estratégia é sempre mantida, mesmo que seja mais velha que o corte.
    Devolve quantas gerações foram apagadas.
    """

    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).replace(
        microsecond=0
    ).isoformat()
    with conectar() as conn:
        ids = [
            linha[0]
            for linha in conn.execute(
                """
                SELECT id FROM rankings
                 WHERE gerado_em < ?
                   AND id NOT IN (
                        SELECT id FROM (
                            SELECT id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY estrategia
                                       ORDER BY gerado_em DESC
                                   ) AS ordem
                              FROM rankings
                        ) WHERE ordem = 1
                   )
                """,
                (corte,),
            )
        ]
        if not ids:
            return 0
        marcadores = ",".join("?" for _ in ids)
        conn.execute(
            f"DELETE FROM ranking_itens WHERE ranking_id IN ({marcadores})", ids
        )
        conn.execute(f"DELETE FROM rankings WHERE id IN ({marcadores})", ids)
        return len(ids)


def finalizar_coleta(id: int, status: str, resumo: dict[str, Any] | None = None) -> None:
    resumo_json = json.dumps(resumo or {}, ensure_ascii=False, default=str)
    with conectar() as conn:
        conn.execute(
            """
            UPDATE coletas
               SET terminado_em = ?, status = ?, resumo_json = ?
             WHERE id = ?
            """,
            (_agora(), status, resumo_json, id),
        )


def gravar_configuracao(chave: str, valor: Any) -> None:
    """Grava um valor em ``configuracoes`` (idempotente por chave).

    ``str`` vai para ``valor`` (texto simples, usado pelo agendamento); os
    demais tipos continuam serializados em ``valor_json`` (usado pelas
    configurações das estratégias).
    """

    texto = valor if isinstance(valor, str) else None
    conteudo = (
        None
        if isinstance(valor, str)
        else json.dumps(valor, ensure_ascii=False, default=str)
    )
    with conectar() as conn:
        conn.execute(
            """
            INSERT INTO configuracoes (chave, valor, valor_json, atualizado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chave) DO UPDATE SET
                valor = excluded.valor,
                valor_json = excluded.valor_json,
                atualizado_em = excluded.atualizado_em
            """,
            (chave, texto, conteudo, _agora()),
        )


def ler_configuracao(chave: str, padrao: str | None = None) -> Any:
    with conectar() as conn:
        linha = conn.execute(
            "SELECT valor, valor_json FROM configuracoes WHERE chave = ?",
            (chave,),
        ).fetchone()
    if linha is None:
        return padrao
    if linha["valor"] is not None:
        return linha["valor"]
    if linha["valor_json"] is None:
        return padrao
    try:
        return json.loads(linha["valor_json"])
    except (TypeError, json.JSONDecodeError):
        return padrao


def tem_dados(ticker: str) -> bool:
    with conectar() as conn:
        row = conn.execute("SELECT 1 FROM cotacoes WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()
        return row is not None


def consultar(sql: str, parametros: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with conectar() as conn:
        return list(conn.execute(sql, tuple(parametros)).fetchall())
