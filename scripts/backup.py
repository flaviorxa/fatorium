"""Backup consistente do banco SQLite, mantendo os 14 arquivos mais recentes.

Uso:
    python scripts/backup.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config

PADRAO = "invest-*.db"
MANTER = 14


def _diretorio_padrao() -> Path:
    return config.RAIZ / "data" / "backups"


def podar_backups(diretorio: Path | str | None = None, manter: int = MANTER) -> list[Path]:
    """Remove os backups mais antigos e devolve os arquivos mantidos."""

    pasta = Path(diretorio) if diretorio is not None else _diretorio_padrao()
    if not pasta.exists():
        return []
    arquivos = sorted(
        (caminho for caminho in pasta.glob(PADRAO) if caminho.is_file()),
        key=lambda caminho: (caminho.name, caminho.stat().st_mtime),
        reverse=True,
    )
    mantidos = arquivos[:manter]
    for caminho in arquivos[manter:]:
        caminho.unlink(missing_ok=True)
    return sorted(mantidos, key=lambda caminho: caminho.name)


def criar_backup(
    destino: Path | str | None = None,
    origem: Path | str | None = None,
    manter: int = MANTER,
    quando: date | None = None,
) -> Path:
    """Copia o banco com ``sqlite3.Connection.backup`` (sem copiar arquivo aberto).

    Devolve o caminho do backup criado. O nome é ``invest-AAAA-MM-DD.db`` no
    diretório ``data/backups`` por padrão.
    """

    pasta = Path(destino) if destino is not None else _diretorio_padrao()
    pasta.mkdir(parents=True, exist_ok=True)
    fonte = Path(origem) if origem is not None else Path(config.DB_PATH)
    if not fonte.exists():
        raise FileNotFoundError(f"banco não encontrado em {fonte}")

    referencia = quando or date.today()
    caminho = pasta / f"invest-{referencia.isoformat()}.db"

    with sqlite3.connect(fonte) as conexao_origem:
        with sqlite3.connect(caminho) as conexao_destino:
            conexao_origem.backup(conexao_destino)

    podar_backups(pasta, manter)
    return caminho


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup consistente do banco.")
    parser.add_argument("--origem", help="banco de origem; padrão: data/invest.db")
    parser.add_argument("--destino", help="diretório de backups; padrão: data/backups")
    parser.add_argument("--manter", type=int, default=MANTER)
    args = parser.parse_args()

    caminho = criar_backup(
        destino=args.destino, origem=args.origem, manter=args.manter
    )
    tamanho = caminho.stat().st_size
    print(f"backup: {caminho}")
    print(f"tamanho: {tamanho} bytes")


if __name__ == "__main__":
    main()
