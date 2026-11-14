import sqlite3
from datetime import date, timedelta

from scripts import backup


def test_backup_e_copia_consistente(tmp_path):
    origem = tmp_path / "origem.db"
    with sqlite3.connect(origem) as conn:
        conn.execute("CREATE TABLE teste (valor TEXT)")
        conn.execute("INSERT INTO teste VALUES ('ok')")

    pasta = tmp_path / "backups"
    caminho = backup.criar_backup(
        destino=pasta,
        origem=origem,
        quando=date(2026, 10, 29),
    )

    assert caminho == pasta / "invest-2026-10-29.db"
    assert caminho.exists()
    with sqlite3.connect(caminho) as conn:
        assert conn.execute("SELECT valor FROM teste").fetchone()[0] == "ok"


def test_poda_mantem_os_quatorze_mais_recentes(tmp_path):
    pasta = tmp_path / "backups"
    pasta.mkdir()
    inicio = date(2026, 1, 1)
    for indice in range(20):
        dia = inicio + timedelta(days=indice)
        (pasta / f"invest-{dia.isoformat()}.db").write_bytes(b"falso")

    mantidos = backup.podar_backups(pasta, manter=14)

    assert len(mantidos) == 14
    assert len(list(pasta.glob("invest-*.db"))) == 14
    assert not (pasta / "invest-2026-01-01.db").exists()
    assert (pasta / "invest-2026-01-20.db").exists()
