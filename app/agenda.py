"""Agendador interno dos jobs de coleta, rankings, frescor e backup.

O agendador vive dentro do processo da aplicação (nada de cron do sistema). Os
jobs nunca deixam uma exceção escapar: o erro é registrado em ``falhas`` e no
log, e o processo continua vivo.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from functools import partial
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config, db

logger = logging.getLogger(__name__)

FUSO = ZoneInfo("America/Sao_Paulo")
JOB_IDS = (
    "cotacoes",
    "fundamentos",
    "consenso",
    "estimativas",
    "rankings",
    "cvm_mensal",
    "frescor",
    "backup",
)
JOBS_COLETA = ("cotacoes", "fundamentos", "consenso", "estimativas", "rankings")
OFFSETS_COLETA = {
    "cotacoes": 0,
    "fundamentos": 35,
    "consenso": 45,
    "estimativas": 60,
    "rankings": 70,
}
FREQUENCIAS = ("dias_uteis", "semanal", "desligado")
FREQUENCIA_PADRAO = "dias_uteis"
HORARIO_PADRAO = "18:35"
TIPOS_COLETA_JOB = {
    "cotacoes": "cotacoes",
    "fundamentos": "fundamentos",
    "consenso": "consenso",
    "estimativas": "estimativas",
    "cvm_mensal": "cvm_mensal",
}

_scheduler: BackgroundScheduler | None = None
_ultimos: dict[str, dict[str, Any]] = {}


def _agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _registrar(nome: str, status: str) -> None:
    _ultimos[nome] = {"ultima_execucao": _agora(), "ultimo_status": status}


def executar_job(nome: str, funcao: Callable[[], Any]) -> dict[str, Any]:
    """Executa um job isolando qualquer exceção.

    É o wrapper chamado pelo agendador (e diretamente pelos testes). Nunca
    levanta: em caso de erro grava em ``falhas`` e devolve ``status='falha'``.
    """

    try:
        resultado = funcao()
    except Exception as exc:  # noqa: BLE001 - o job não pode derrubar o processo
        erro = f"{type(exc).__name__}: {exc}"
        logger.exception("job %s falhou", nome)
        try:
            db.registrar_falha("agenda", None, nome, erro)
        except Exception:  # pragma: no cover - falha do próprio registro
            logger.exception("não foi possível registrar a falha do job %s", nome)
        _registrar(nome, "falha")
        return {"status": "falha", "erro": erro}

    status = "ok"
    if isinstance(resultado, dict) and resultado.get("status"):
        status = str(resultado["status"])
    _registrar(nome, status)
    return {"status": status, "resultado": resultado}


def _tickers_do_universo() -> list[str]:
    return list(config.ler_universo().keys())


def _job_rankings() -> dict[str, Any]:
    from app import alertas
    from app.motor import rankings

    resumo = rankings.gerar_rankings()
    apagadas = db.podar_rankings()
    if apagadas:
        logger.info("rankings antigos apagados: %d gerações", apagadas)
    mudancas = alertas.gerar_alertas_rankings()
    falhas = alertas.gerar_alertas_falhas()
    logger.info(
        "rankings gerados; alertas entrou=%d saiu=%d falha_coleta=%d",
        mudancas["entrou"],
        mudancas["saiu"],
        falhas,
    )
    return {
        "rankings": {nome: len(dados["itens"]) for nome, dados in resumo.items()},
        "alertas": {**mudancas, "falha_coleta": falhas},
    }


def _executar_coleta(
    tipo: str,
    *,
    somente_fundamentos: bool = False,
    periodo: str = "5d",
) -> dict[str, Any]:
    from scripts.coletar import coletar

    id_coleta = db.abrir_coleta_ou_recusar(tipo)
    if id_coleta is None:
        logger.info("coleta já em andamento: %s recusada", tipo)
        return {"status": "recusado", "tipo": tipo}

    _registrar(tipo, "em_andamento")
    resumo: dict[str, Any] = {}
    status = "falha"
    try:
        resumo = coletar(
            _tickers_do_universo(),
            periodo,
            somente_fundamentos=somente_fundamentos,
        )
        if somente_fundamentos:
            erros = int(resumo.get("falhas", 0))
            status = "ok" if erros == 0 else "parcial"
        else:
            barras = sum(
                int(item.get("barras") or 0)
                for item in resumo.get("por_ticker", {}).values()
            )
            if barras == 0:
                logger.info("coleta %s: sem_barra_nova", tipo)
            status = "ok" if int(resumo.get("falhas", 0)) == 0 else "parcial"
            resumo["sem_barra_nova"] = barras == 0
        return {"status": status, "tipo": tipo, "coleta": id_coleta}
    except Exception as exc:  # noqa: BLE001 - registra e deixa o finally finalizar
        resumo = {"erro": f"{type(exc).__name__}: {exc}"}
        db.registrar_falha("sistema", None, tipo, resumo["erro"])
        raise
    finally:
        db.finalizar_coleta(id_coleta, status, resumo)


def _job_cotacoes() -> dict[str, Any]:
    resultado: dict[str, Any] | None = None
    try:
        resultado = _executar_coleta("cotacoes", periodo="5d")
        return resultado
    finally:
        if resultado is None or resultado.get("status") != "recusado":
            _job_rankings()


def _job_fundamentos() -> dict[str, Any]:
    resultado: dict[str, Any] | None = None
    try:
        resultado = _executar_coleta(
            "fundamentos", somente_fundamentos=True, periodo="5d"
        )
        return resultado
    finally:
        if resultado is None or resultado.get("status") != "recusado":
            _job_rankings()


def _job_consenso() -> dict[str, Any]:
    from scripts.coletar import coletar_consenso

    id_coleta = db.abrir_coleta_ou_recusar("consenso")
    if id_coleta is None:
        logger.info("coleta já em andamento: consenso recusada")
        return {"status": "recusado", "tipo": "consenso"}

    _registrar("consenso", "em_andamento")
    resumo: dict[str, Any] = {}
    status = "falha"
    try:
        resumo = coletar_consenso(_tickers_do_universo())
        status = "ok" if int(resumo.get("falhas", 0)) == 0 else "parcial"
        return {"status": status, "coleta": id_coleta, **resumo}
    except Exception as exc:  # noqa: BLE001
        resumo = {"erro": f"{type(exc).__name__}: {exc}"}
        db.registrar_falha("msn", None, "consenso", resumo["erro"])
        raise
    finally:
        db.finalizar_coleta(id_coleta, status, resumo)


def _job_estimativas() -> dict[str, Any]:
    """Preços-alvo do BTG e da Genial, um depois do outro (uma coleta só)."""

    from scripts.coletar import coletar_btg, coletar_genial

    id_coleta = db.abrir_coleta_ou_recusar("estimativas")
    if id_coleta is None:
        logger.info("coleta já em andamento: estimativas recusada")
        return {"status": "recusado", "tipo": "estimativas"}

    _registrar("estimativas", "em_andamento")
    resumo: dict[str, Any] = {}
    falhas = 0
    status = "falha"
    try:
        for nome, coletor in (("btg", coletar_btg), ("genial", coletar_genial)):
            try:
                parcial = coletor(_tickers_do_universo())
                resumo[nome] = parcial
                falhas += int(parcial.get("falhas", 0))
            except Exception as exc:  # noqa: BLE001 - uma fonte não derruba a outra
                erro = f"{type(exc).__name__}: {exc}"
                db.registrar_falha(nome, None, "preco_alvo", erro)
                resumo[nome] = {"erro": erro}
                falhas += 1
        status = "ok" if falhas == 0 else "parcial"
        return {"status": status, "coleta": id_coleta, "falhas": falhas, **resumo}
    finally:
        db.finalizar_coleta(id_coleta, status, resumo)


def _recarregar_cvm(ano: int) -> dict[str, int]:
    from app.fontes import cvm
    from scripts.coletar import _campos_cvm, _ler_mapa_fii

    diretorio = cvm.baixar_mensal(ano)
    fundos = cvm.ler_fundos(ano, diretorio)
    mapa = _ler_mapa_fii()

    # Fi-Infra, Fiagro e FIP-Infra não estão no informe mensal de FII
    faltando = {
        str(entrada["cnpj"]) for entrada in mapa.values() if entrada.get("cnpj")
    } - set(fundos)
    alternativos = (
        cvm.ler_fundos_alternativos(ano, faltando) if faltando else {}
    )

    gravados = 0
    for ticker, entrada in mapa.items():
        cnpj = str(entrada.get("cnpj"))
        fundo = fundos.get(cnpj) or alternativos.get(cnpj)
        if not fundo or not fundo.get("data_ref"):
            continue
        gravados += db.gravar_fundos(
            ticker, fundo["data_ref"], _campos_cvm(fundo), "cvm"
        )
    return {
        "fundos": len(fundos),
        "alternativos": len(alternativos),
        "fundamentos": gravados,
    }


def _job_cvm_mensal() -> dict[str, Any]:
    id_coleta = db.abrir_coleta_ou_recusar("cvm_mensal")
    if id_coleta is None:
        logger.info("coleta já em andamento: cvm_mensal recusada")
        return {"status": "recusado", "tipo": "cvm_mensal"}

    _registrar("cvm_mensal", "em_andamento")
    resumo: dict[str, Any] = {}
    status = "falha"
    try:
        resumo = _recarregar_cvm(date.today().year)
        status = "ok"
        return {"status": status, "coleta": id_coleta, **resumo}
    except Exception as exc:  # noqa: BLE001
        resumo = {"erro": f"{type(exc).__name__}: {exc}"}
        db.registrar_falha("cvm", None, "mensal", resumo["erro"])
        raise
    finally:
        db.finalizar_coleta(id_coleta, status, resumo)


def _job_frescor() -> dict[str, Any]:
    from app import alertas

    desatualizados = alertas.gerar_alertas_desatualizados()
    falhas = alertas.gerar_alertas_falhas()
    logger.info(
        "frescor: alertas desatualizado=%d falha_coleta=%d",
        desatualizados,
        falhas,
    )
    return {"desatualizado": desatualizados, "falha_coleta": falhas}


def _job_backup() -> dict[str, Any]:
    from scripts import backup

    caminho = backup.criar_backup()
    return {"status": "ok", "backup": str(caminho)}


JOBS: dict[str, Callable[[], Any]] = {
    "cotacoes": _job_cotacoes,
    "fundamentos": _job_fundamentos,
    "consenso": _job_consenso,
    "estimativas": _job_estimativas,
    "rankings": _job_rankings,
    "cvm_mensal": _job_cvm_mensal,
    "frescor": _job_frescor,
    "backup": _job_backup,
}


def _normalizar_horario(horario: str) -> str:
    """Valida ``HH:MM`` (aceitando ``H:MM``) e devolve com zero à esquerda."""

    texto = str(horario or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", texto):
        raise ValueError("horário inválido: use HH:MM, por exemplo 18:35")
    hora_texto, minuto_texto = texto.split(":")
    hora, minuto = int(hora_texto), int(minuto_texto)
    if not 0 <= hora <= 22 or not 0 <= minuto <= 59:
        raise ValueError("horário inválido: use uma hora entre 00:00 e 22:45")
    if hora == 22 and minuto > 45:
        raise ValueError("horário acima de 22:45: a cadeia passaria da meia-noite")
    return f"{hora:02d}:{minuto:02d}"


def validar_agendamento(frequencia: str, horario: str) -> tuple[str, str]:
    """Normaliza e valida a configuração; levanta ``ValueError`` se inválida."""

    if frequencia not in FREQUENCIAS:
        raise ValueError(
            "frequência inválida: escolha dias_uteis, semanal ou desligado"
        )
    return frequencia, _normalizar_horario(horario)


def agendamento() -> dict[str, str]:
    """Configuração gravada, caindo no padrão se estiver ausente ou corrompida."""

    frequencia = db.ler_configuracao("agenda_frequencia", FREQUENCIA_PADRAO)
    horario = db.ler_configuracao("agenda_horario", HORARIO_PADRAO)
    try:
        frequencia, horario = validar_agendamento(
            str(frequencia), str(horario)
        )
    except ValueError:
        logger.warning(
            "configuração de agendamento inválida (%r, %r); usando o padrão",
            frequencia,
            horario,
        )
        return {"frequencia": FREQUENCIA_PADRAO, "horario": HORARIO_PADRAO}
    return {"frequencia": frequencia, "horario": horario}


def _gatilhos_coleta(frequencia: str, horario: str) -> dict[str, Any]:
    """Gatilhos dos cinco jobs da cadeia de coleta (vazio se desligado)."""

    if frequencia == "desligado":
        return {}

    _, horario = validar_agendamento(frequencia, horario)
    hora, minuto = (int(parte) for parte in horario.split(":"))
    inicio = hora * 60 + minuto
    dias = "mon" if frequencia == "semanal" else "mon-fri"

    gatilhos: dict[str, Any] = {}
    for job_id in JOBS_COLETA:
        total = inicio + OFFSETS_COLETA[job_id]
        hora_job, minuto_job = divmod(total, 60)
        gatilhos[job_id] = CronTrigger(
            day_of_week="mon" if job_id == "consenso" else dias,
            hour=hora_job,
            minute=minuto_job,
            timezone=FUSO,
        )
    return gatilhos


def criar_scheduler() -> BackgroundScheduler:
    """Cria (sem iniciar) o agendador com os jobs da rotina."""

    scheduler = BackgroundScheduler(
        timezone=FUSO,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 3600,
        },
    )
    configuracao = agendamento()
    gatilhos = _gatilhos_coleta(
        configuracao["frequencia"], configuracao["horario"]
    )
    gatilhos.update(
        {
            "cvm_mensal": CronTrigger(day=5, hour=7, minute=0, timezone=FUSO),
            "frescor": CronTrigger(hour=8, minute=10, timezone=FUSO),
            "backup": CronTrigger(hour=3, minute=20, timezone=FUSO),
        }
    )
    for job_id in JOB_IDS:
        if job_id not in gatilhos:
            continue
        scheduler.add_job(
            partial(executar_job, job_id, JOBS[job_id]),
            gatilhos[job_id],
            id=job_id,
            name=job_id,
            replace_existing=True,
        )
    return scheduler


def iniciar_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    _scheduler = criar_scheduler()
    _scheduler.start()
    logger.info("agendador iniciado com %d jobs", len(_scheduler.get_jobs()))
    return _scheduler


def parar_scheduler() -> None:
    global _scheduler
    scheduler = _scheduler
    _scheduler = None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("agendador encerrado")


def aplicar_agendamento() -> list[str]:
    """Aplica a configuração gravada ao agendador em execução, sem reiniciar.

    Devolve os ids dos jobs da cadeia que foram recolocados ou removidos; sem
    agendador em execução, devolve lista vazia.
    """

    scheduler = _scheduler
    if scheduler is None or not scheduler.running:
        return []

    configuracao = agendamento()
    desligado = configuracao["frequencia"] == "desligado"
    gatilhos = _gatilhos_coleta(
        configuracao["frequencia"], configuracao["horario"]
    )

    afetados: list[str] = []
    for job_id in JOBS_COLETA:
        if desligado:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)
                afetados.append(job_id)
            continue
        scheduler.add_job(
            partial(executar_job, job_id, JOBS[job_id]),
            gatilhos[job_id],
            id=job_id,
            name=job_id,
            replace_existing=True,
        )
        afetados.append(job_id)
    logger.info("agendamento aplicado; jobs afetados: %s", afetados)
    return afetados


def _iso(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.isoformat()
    return str(valor)


def status() -> list[dict[str, Any]]:
    """Estado dos jobs: próxima execução, última execução e último status.

    Para os jobs de coleta, o ``ultimo_status`` vem do registro mais recente em
    ``coletas`` — inclusive ``em_andamento``, que ainda não passou pelo wrapper.
    """

    proximas: dict[str, Any] = {}
    if _scheduler is not None and _scheduler.running:
        for job in _scheduler.get_jobs():
            proximas[job.id] = getattr(job, "next_run_time", None)

    coletas: dict[str, Any] = {}
    with db.conectar() as conn:
        for tipo in set(TIPOS_COLETA_JOB.values()):
            coletas[tipo] = conn.execute(
                """
                SELECT status, iniciado_em, terminado_em
                  FROM coletas
                 WHERE tipo = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (tipo,),
            ).fetchone()

    resultado = []
    for job_id in JOB_IDS:
        ultimo = dict(_ultimos.get(job_id, {}))
        coleta = coletas.get(TIPOS_COLETA_JOB.get(job_id, ""))
        if coleta is not None:
            ultimo["ultimo_status"] = coleta["status"]
            ultimo["ultima_execucao"] = (
                coleta["terminado_em"] or coleta["iniciado_em"]
            )
        resultado.append(
            {
                "job": job_id,
                "proxima_execucao": _iso(proximas.get(job_id)),
                "ultima_execucao": ultimo.get("ultima_execucao"),
                "ultimo_status": ultimo.get("ultimo_status"),
            }
        )
    return resultado
