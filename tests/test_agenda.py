from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import agenda, config, db


def _usar_banco_temporario(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "invest.db")


def _campos(trigger):
    return {campo.name: str(campo) for campo in trigger.fields}


def test_jobs_registrados_com_os_gatilhos_esperados(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    scheduler = agenda.criar_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == set(agenda.JOB_IDS)

    esperados = {
        "cotacoes": ("mon-fri", "18", "35", "*"),
        "fundamentos": ("mon-fri", "19", "10", "*"),
        "consenso": ("mon", "19", "20", "*"),
        "estimativas": ("mon-fri", "19", "35", "*"),
        "rankings": ("mon-fri", "19", "45", "*"),
        "cvm_mensal": ("*", "7", "0", "5"),
        "frescor": ("*", "8", "10", "*"),
        "backup": ("*", "3", "20", "*"),
    }
    for job_id, (dia_semana, hora, minuto, dia) in esperados.items():
        campos = _campos(jobs[job_id].trigger)
        assert campos["day_of_week"] == dia_semana
        assert campos["hour"] == hora
        assert campos["minute"] == minuto
        assert campos["day"] == dia


def test_horario_configurado_desloca_a_cadeia(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.gravar_configuracao("agenda_frequencia", "dias_uteis")
    db.gravar_configuracao("agenda_horario", "08:00")

    jobs = {job.id: job for job in agenda.criar_scheduler().get_jobs()}

    esperados = {
        "cotacoes": ("mon-fri", "8", "0"),
        "fundamentos": ("mon-fri", "8", "35"),
        "consenso": ("mon", "8", "45"),
        "estimativas": ("mon-fri", "9", "0"),
        "rankings": ("mon-fri", "9", "10"),
    }
    for job_id, (dia_semana, hora, minuto) in esperados.items():
        campos = _campos(jobs[job_id].trigger)
        assert campos["day_of_week"] == dia_semana
        assert campos["hour"] == hora
        assert campos["minute"] == minuto


def test_frequencia_semanal_coloca_a_cadeia_na_segunda(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.gravar_configuracao("agenda_frequencia", "semanal")
    db.gravar_configuracao("agenda_horario", "19:00")

    jobs = {job.id: job for job in agenda.criar_scheduler().get_jobs()}

    for job_id in agenda.JOBS_COLETA:
        assert _campos(jobs[job_id].trigger)["day_of_week"] == "mon"


def test_frequencia_desligada_remove_so_a_cadeia(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.gravar_configuracao("agenda_frequencia", "desligado")
    db.gravar_configuracao("agenda_horario", "18:35")

    jobs = {job.id for job in agenda.criar_scheduler().get_jobs()}

    assert jobs == {"cvm_mensal", "frescor", "backup"}


def test_validar_agendamento_normaliza_e_rejeita_invalidos():
    assert agenda.validar_agendamento("dias_uteis", "7:05") == (
        "dias_uteis",
        "07:05",
    )
    assert agenda.validar_agendamento("semanal", "22:45") == (
        "semanal",
        "22:45",
    )
    for horario in ("25:00", "22:50", "abc", "18:5", "18:60"):
        try:
            agenda.validar_agendamento("dias_uteis", horario)
        except ValueError:
            continue
        raise AssertionError(f"horário aceito indevidamente: {horario}")
    try:
        agenda.validar_agendamento("mensal", "18:35")
    except ValueError:
        pass
    else:
        raise AssertionError("frequência inválida aceita")


def test_configuracao_corrompida_volta_ao_padrao(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    with db.conectar() as conn:
        conn.execute(
            "INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES (?, ?, ?)",
            ("agenda_horario", "99:99", "2026-01-01T00:00:00+00:00"),
        )

    assert agenda.agendamento() == {
        "frequencia": "dias_uteis",
        "horario": "18:35",
    }


def test_duas_chamadas_imediatas_abrem_uma_coleta_so(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    instante = datetime(2026, 10, 29, 14, 56, tzinfo=timezone.utc)

    primeiro = db.abrir_coleta_ou_recusar("cotacoes", agora=instante)
    segundo = db.abrir_coleta_ou_recusar("cotacoes", agora=instante)

    assert primeiro is not None
    assert segundo is None
    with db.conectar() as conn:
        total = conn.execute("SELECT COUNT(*) FROM coletas").fetchone()[0]
    assert total == 1


def test_abrir_coleta_recusa_segunda_e_libera_apos_trinta_minutos(
    monkeypatch, tmp_path
):
    _usar_banco_temporario(monkeypatch, tmp_path)
    inicio = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)

    primeiro = db.abrir_coleta_ou_recusar("cotacoes", agora=inicio)
    assert primeiro is not None
    assert (
        db.abrir_coleta_ou_recusar(
            "cotacoes", agora=inicio + timedelta(minutes=10)
        )
        is None
    )

    segundo = db.abrir_coleta_ou_recusar(
        "cotacoes", agora=inicio + timedelta(minutes=31)
    )
    assert segundo is not None
    assert segundo != primeiro

    with db.conectar() as conn:
        linhas = conn.execute(
            "SELECT id, status, terminado_em FROM coletas ORDER BY id"
        ).fetchall()
    assert linhas[0]["status"] == "interrompida"
    assert linhas[0]["terminado_em"] is not None
    assert linhas[1]["status"] == "em_andamento"


def test_encerrar_coletas_orfas_e_idempotente(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.iniciar_coleta("cotacoes")
    db.iniciar_coleta("fundamentos")

    assert db.encerrar_coletas_orfas() == 2

    coletas = db.consultar(
        "SELECT status, terminado_em FROM coletas ORDER BY id"
    )
    assert [linha["status"] for linha in coletas] == [
        "interrompida",
        "interrompida",
    ]
    assert all(linha["terminado_em"] is not None for linha in coletas)

    falhas = db.consultar(
        "SELECT fonte, etapa, erro FROM falhas WHERE fonte = 'sistema'"
    )
    assert len(falhas) == 2
    assert {linha["etapa"] for linha in falhas} == {"coleta"}
    assert {linha["erro"] for linha in falhas} == {
        "processo encerrado durante a coleta"
    }

    assert db.encerrar_coletas_orfas() == 0
    assert (
        db.consultar("SELECT COUNT(*) AS total FROM falhas")[0]["total"] == 2
    )


def test_job_cotacoes_recusado_nao_grava_falha(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.iniciar_coleta("manual")

    resultado = agenda._job_cotacoes()

    assert resultado == {"status": "recusado", "tipo": "cotacoes"}
    assert db.consultar("SELECT * FROM falhas") == []


def test_status_mostra_coleta_em_andamento(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)
    db.abrir_coleta_ou_recusar("cotacoes")

    por_job = {item["job"]: item for item in agenda.status()}

    assert por_job["cotacoes"]["ultimo_status"] == "em_andamento"
    assert por_job["cotacoes"]["ultima_execucao"] is not None


def test_pacote_app_nao_usa_status_antigo():
    raiz = Path(agenda.__file__).resolve().parent
    for arquivo in sorted(raiz.rglob("*")):
        if arquivo.is_file() and arquivo.suffix in {".py", ".html", ".sql"}:
            assert "rodando" not in arquivo.read_text(encoding="utf-8").lower(), arquivo


def test_excecao_em_job_nao_derruba_o_processo(monkeypatch, tmp_path):
    _usar_banco_temporario(monkeypatch, tmp_path)

    def explode():
        raise RuntimeError("falha proposital")

    resultado = agenda.executar_job("teste", explode)

    assert resultado["status"] == "falha"
    assert "falha proposital" in resultado["erro"]
    falhas = db.consultar(
        "SELECT fonte, etapa, erro FROM falhas WHERE fonte = 'agenda'"
    )
    assert len(falhas) == 1
    assert falhas[0]["etapa"] == "teste"
