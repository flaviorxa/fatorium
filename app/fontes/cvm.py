"""Dados abertos mensais de FIIs publicados pela CVM.

O informe mensal de FII ``inf_mensal_fii_{ANO}.zip`` é um arquivo anual
acumulado com três CSVs. Este módulo usa apenas dois:

- ``inf_mensal_fii_geral_{ANO}.csv`` (nome, segmento, ISIN);
- ``inf_mensal_fii_complemento_{ANO}.csv`` (patrimônio, cotistas, taxa).

Os arquivos usam separador ``;``, encoding ``latin-1`` e têm várias linhas por
fundo (uma por mês/versão). O índice devolvido mantém a ``Data_Referencia``
mais recente de cada CNPJ.

Fi-Infra (FI), Fiagro e FIP-Infra não estão no informe mensal de FII e têm o
valor patrimonial da cota em outros arquivos da CVM: ``INF_DIARIO`` (FI),
``INF_MENSAL_FIAGRO`` (Fiagro) e ``INF_QUADRIMESTRAL_FIP`` (FIP-Infra).
``ler_fundos_alternativos`` cobre esses três casos.
"""

from __future__ import annotations

import csv
import re
import time
import unicodedata
import zipfile
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

URL_MENSAL = (
    "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/"
    "inf_mensal_fii_{ano}.zip"
)
DIRETORIO = Path("data/cvm")
VALIDADE_CACHE_S = 7 * 24 * 60 * 60
TIMEOUT_S = 120.0

_CSV_GERAL = "inf_mensal_fii_geral_{ano}.csv"
_CSV_COMPLEMENTO = "inf_mensal_fii_complemento_{ano}.csv"


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _numero(valor: Any) -> float | None:
    """Converte número do CSV da CVM; campo vazio vira ``None`` (nunca 0)."""

    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


CAMPO_TAXA_ADMINISTRACAO = "Percentual_Despesas_Taxa_Administracao"
TEXTO_TAXA_ADMINISTRACAO = (
    "percentual de despesas informado pela CVM, como publicado; "
    "não é a taxa de administração contratual"
)


def _taxa_administracao(valor: Any) -> float | None:
    """Guarda ``Percentual_Despesas_Taxa_Administracao`` exatamente como veio.

    A CVM publica ora fração (``0,012``), ora percentual (``1,2``), e a unidade
    não vem declarada no arquivo. Não há como adivinhar sem inventar dado:
    devolvemos o número bruto e o nome do campo de origem. Campo vazio
    continua ``None``.
    """

    return _numero(valor)


def _mais_recente(novo: dict[str, Any], atual: dict[str, Any] | None) -> bool:
    if atual is None:
        return True
    chave_nova = (
        str(novo.get("Data_Referencia") or ""),
        _numero(novo.get("Versao")) or 0.0,
    )
    chave_atual = (
        str(atual.get("Data_Referencia") or ""),
        _numero(atual.get("Versao")) or 0.0,
    )
    return chave_nova > chave_atual


def _ler_ultimas(caminho: Path) -> dict[str, dict[str, str]]:
    """Lê um CSV da CVM e mantém a linha mais recente por CNPJ."""

    ultimas: dict[str, dict[str, str]] = {}
    with caminho.open("r", encoding="latin-1", newline="") as arquivo:
        leitor = csv.DictReader(arquivo, delimiter=";")
        for linha in leitor:
            cnpj = _texto(linha.get("CNPJ_Fundo_Classe"))
            if cnpj is None:
                continue
            registros = {str(k): v for k, v in linha.items() if k is not None}
            if _mais_recente(registros, ultimas.get(cnpj)):
                ultimas[cnpj] = registros
    return ultimas


def _baixar(url: str, destino: Path) -> None:
    """Baixa o ZIP com timeout de 120 s e uma nova tentativa em falha de rede."""

    temporario = destino.with_suffix(destino.suffix + ".part")
    ultimo: Exception | None = None
    for tentativa in range(2):
        try:
            with httpx.Client(
                timeout=TIMEOUT_S, follow_redirects=True, max_redirects=5
            ) as cliente:
                with cliente.stream("GET", url) as resposta:
                    resposta.raise_for_status()
                    with temporario.open("wb") as arquivo:
                        for bloco in resposta.iter_bytes():
                            arquivo.write(bloco)
            temporario.replace(destino)
            return
        except Exception as exc:  # noqa: BLE001 - a última falha é propagada
            ultimo = exc
            temporario.unlink(missing_ok=True)
            if tentativa == 0:
                time.sleep(1.0)
    assert ultimo is not None
    raise ultimo


def _extrair(zip_path: Path, destino: Path, arquivos: Iterable[str]) -> None:
    with zipfile.ZipFile(zip_path) as arquivo_zip:
        nomes = {Path(nome).name: nome for nome in arquivo_zip.namelist()}
        for nome in arquivos:
            origem = nomes.get(nome)
            if origem is None:
                continue
            with arquivo_zip.open(origem) as origem_zip:
                with (destino / nome).open("wb") as saida:
                    while True:
                        bloco = origem_zip.read(1024 * 1024)
                        if not bloco:
                            break
                        saida.write(bloco)


def baixar_mensal(ano: int, destino: str | Path | None = None) -> Path:
    """Baixa (se necessário) e extrai os dois CSVs mensais de FII do ano.

    O ZIP é reutilizado por até 7 dias. Depois disso é baixado de novo. Os
    CSVs são extraídos para ``destino`` e o diretório é devolvido.
    """

    diretorio = Path(destino) if destino is not None else DIRETORIO
    diretorio.mkdir(parents=True, exist_ok=True)

    zip_path = diretorio / f"inf_mensal_fii_{ano}.zip"
    idade = (
        time.time() - zip_path.stat().st_mtime if zip_path.exists() else None
    )
    if idade is None or idade > VALIDADE_CACHE_S:
        _baixar(URL_MENSAL.format(ano=ano), zip_path)

    _extrair(
        zip_path,
        diretorio,
        (_CSV_GERAL.format(ano=ano), _CSV_COMPLEMENTO.format(ano=ano)),
    )
    return diretorio


_TERMOS_GENERICOS = {
    "FII",
    "FUNDO",
    "DE",
    "INVESTIMENTO",
    "IMOBILIARIO",
    "RESPONSABILIDADE",
    "LIMITADA",
}


def normalizar_nome(nome: Any) -> str:
    """Maiúsculas, sem acento/pontuação e sem os termos genéricos de FII."""

    texto = unicodedata.normalize("NFKD", str(nome or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c)).upper()
    texto = re.sub(r"[^A-Z0-9]+", " ", texto)
    tokens = [t for t in texto.split() if t not in _TERMOS_GENERICOS]
    return " ".join(tokens)


def casar_por_nome(
    nome: Any, fundos: dict[str, dict[str, Any]], limiar: float = 0.90
) -> str | None:
    """CNPJ do fundo da CVM cujo nome mais se parece com ``nome``.

    Serve para FII novo no universo: o mapa ticker→CNPJ é fixo, e sem isso o
    fundo entra sem valor patrimonial. Mesma regra do script de mapeamento:
    similaridade >= 0,90, senão devolve ``None`` (melhor nada que o fundo
    errado).
    """

    alvo = normalizar_nome(nome)
    if not alvo:
        return None
    melhor: tuple[float, str] | None = None
    for cnpj, dados in fundos.items():
        normalizado = normalizar_nome(dados.get("nome"))
        if not normalizado:
            continue
        similaridade = SequenceMatcher(None, alvo, normalizado).ratio()
        if melhor is None or similaridade > melhor[0]:
            melhor = (similaridade, cnpj)
    if melhor is None or melhor[0] < limiar:
        return None
    return melhor[1]


def ler_fundos(
    ano: int, destino: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Índice por CNPJ com os dados do informe mensal de FII.

    Cada valor tem ``cnpj``, ``nome``, ``data_ref``, ``segmento``, ``vpa``,
    ``patrimonio_liquido``, ``cotas``, ``cotistas``,
    ``taxa_administracao_informe``, ``taxa_administracao_campo`` e ``isin``.
    ``taxa_administracao_informe`` é o valor bruto publicado (sem conversão) e
    ``taxa_administracao_campo`` identifica o campo de origem. Campos vazios
    ficam ``None``.
    """

    diretorio = Path(destino) if destino is not None else DIRETORIO
    caminho_geral = diretorio / _CSV_GERAL.format(ano=ano)
    caminho_complemento = diretorio / _CSV_COMPLEMENTO.format(ano=ano)

    for caminho in (caminho_geral, caminho_complemento):
        if not caminho.exists():
            raise FileNotFoundError(
                f"CSV da CVM não encontrado em {caminho}; rode baixar_mensal({ano})"
            )

    geral = _ler_ultimas(caminho_geral)
    complemento = _ler_ultimas(caminho_complemento)

    fundos: dict[str, dict[str, Any]] = {}
    for cnpj in set(geral) | set(complemento):
        linha_geral = geral.get(cnpj, {})
        linha_complemento = complemento.get(cnpj, {})
        data_ref = _texto(linha_geral.get("Data_Referencia")) or _texto(
            linha_complemento.get("Data_Referencia")
        )
        fundos[cnpj] = {
            "cnpj": cnpj,
            "nome": _texto(linha_geral.get("Nome_Fundo_Classe")),
            "data_ref": data_ref,
            "segmento": _texto(linha_geral.get("Segmento_Atuacao")),
            "vpa": _numero(linha_complemento.get("Valor_Patrimonial_Cotas")),
            "patrimonio_liquido": _numero(
                linha_complemento.get("Patrimonio_Liquido")
            ),
            "cotas": _numero(linha_complemento.get("Cotas_Emitidas")),
            "cotistas": _numero(linha_complemento.get("Total_Numero_Cotistas")),
            "taxa_administracao_informe": _taxa_administracao(
                linha_complemento.get(CAMPO_TAXA_ADMINISTRACAO)
            ),
            "taxa_administracao_campo": CAMPO_TAXA_ADMINISTRACAO,
            "isin": _texto(linha_geral.get("Codigo_ISIN")),
        }
    return fundos


# --- Fi-Infra (FI), Fiagro e FIP-Infra -------------------------------------
#
# Esses fundos não entram no informe mensal de FII. O valor patrimonial da cota
# vem de outros arquivos abertos da CVM:
# - FI (Fi-Infra): INF_DIARIO, ``VL_QUOTA`` (cota patrimonial, não preço);
# - Fiagro: INF_MENSAL_FIAGRO, ``Valor_Patrimonial_Cota``;
# - FIP-Infra: INF_QUADRIMESTRAL_FIP, ``VL_QUOTA_CLASSE``.

URL_DIARIO = (
    "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/"
    "inf_diario_fi_{ano_mes}.zip"
)
URL_FIAGRO = (
    "https://dados.cvm.gov.br/dados/FIAGRO/DOC/INF_MENSAL/DADOS/"
    "inf_mensal_fiagro_{ano_mes}.zip"
)
URL_FIP = (
    "https://dados.cvm.gov.br/dados/FIP/DOC/INF_QUADRIMESTRAL/DADOS/"
    "inf_quadrimestral_fip_{ano}.csv"
)
VALIDADE_DIARIO_S = 24 * 60 * 60


def _formatar_cnpj(valor: Any) -> str | None:
    """``48903610000121`` -> ``48.903.610/0001-21`` (o Fiagro vem sem máscara)."""

    digitos = "".join(c for c in str(valor or "") if c.isdigit())
    if len(digitos) != 14:
        return None
    return (
        f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/"
        f"{digitos[8:12]}-{digitos[12:]}"
    )


def _ler_ultima_por_cnpj(
    caminho: Path, coluna_cnpj: str, coluna_data: str
) -> dict[str, dict[str, str]]:
    """Linha mais recente de cada CNPJ em um CSV da CVM (``;`` e latin-1)."""

    ultimas: dict[str, dict[str, str]] = {}
    with caminho.open("r", encoding="latin-1", newline="") as arquivo:
        for linha in csv.DictReader(arquivo, delimiter=";"):
            cnpj = _formatar_cnpj(linha.get(coluna_cnpj))
            if cnpj is None:
                continue
            registros = {str(k): v for k, v in linha.items() if k is not None}
            atual = ultimas.get(cnpj)
            if atual is None or str(registros.get(coluna_data) or "") > str(
                atual.get(coluna_data) or ""
            ):
                ultimas[cnpj] = registros
    return ultimas


def _meses_recentes() -> list[str]:
    """Mês corrente e o anterior em ``AAAAMM`` (a CVM publica com atraso)."""

    hoje = date.today()
    anterior = date(hoje.year - 1, 12, 1) if hoje.month == 1 else date(
        hoje.year, hoje.month - 1, 1
    )
    return [hoje.strftime("%Y%m"), anterior.strftime("%Y%m")]


def _baixar_e_extrair(url: str, destino: Path, arquivos: Iterable[str]) -> None:
    zip_path = destino.with_suffix(".zip")
    _baixar(url, zip_path)
    _extrair(zip_path, destino.parent, arquivos)


def ler_diario_fi(
    ano_mes: str, destino: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Cota patrimonial (``VL_QUOTA``) dos fundos de investimento do mês."""

    diretorio = Path(destino) if destino is not None else DIRETORIO
    nome = f"inf_diario_fi_{ano_mes}.csv"
    caminho = diretorio / nome
    if not caminho.exists():
        raise FileNotFoundError(f"CSV da CVM não encontrado em {caminho}")

    diario = {}
    for cnpj, linha in _ler_ultima_por_cnpj(
        caminho, "CNPJ_FUNDO_CLASSE", "DT_COMPTC"
    ).items():
        diario[cnpj] = {
            "cnpj": cnpj,
            "nome": None,
            "data_ref": _texto(linha.get("DT_COMPTC")),
            "segmento": None,
            "vpa": _numero(linha.get("VL_QUOTA")),
            "patrimonio_liquido": _numero(linha.get("VL_PATRIM_LIQ")),
            "cotas": None,
            # 0 no diário significa "não informado", nunca zero cotista
            "cotistas": _numero(linha.get("NR_COTST")) or None,
        }
    return diario


def ler_fiagro(
    ano_mes: str, destino: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Valor patrimonial da cota dos Fiagro no informe mensal."""

    diretorio = Path(destino) if destino is not None else DIRETORIO
    principal = diretorio / f"inf_mensal_fiagro_{ano_mes}.csv"
    subclasse = diretorio / f"inf_mensal_fiagro_subclasse_{ano_mes}.csv"
    if not principal.exists() or not subclasse.exists():
        raise FileNotFoundError(f"CSV do Fiagro não encontrado em {diretorio}")

    cotistas = _ler_ultima_por_cnpj(
        principal, "CNPJ_Classe", "Data_Referencia"
    )
    fundos: dict[str, dict[str, Any]] = {}
    for cnpj, linha in _ler_ultima_por_cnpj(
        subclasse, "CNPJ_Classe", "Data_Referencia"
    ).items():
        dono = cotistas.get(cnpj, {})
        fundos[cnpj] = {
            "cnpj": cnpj,
            "nome": _texto(linha.get("Nome_Classe"))
            or _texto(dono.get("Nome_Classe")),
            "data_ref": _texto(linha.get("Data_Referencia")),
            "segmento": None,
            "vpa": _numero(linha.get("Valor_Patrimonial_Cota")),
            "patrimonio_liquido": None,
            "cotas": _numero(linha.get("Numero_Cotas")),
            "cotistas": _numero(dono.get("Numero_Cotistas")) or None,
        }
    return fundos


def ler_fip(
    ano: int, destino: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Valor patrimonial da cota dos FIP no informe quadrimestral."""

    diretorio = Path(destino) if destino is not None else DIRETORIO
    caminho = diretorio / f"inf_quadrimestral_fip_{ano}.csv"
    if not caminho.exists():
        raise FileNotFoundError(f"CSV da CVM não encontrado em {caminho}")

    fundos: dict[str, dict[str, Any]] = {}
    for cnpj, linha in _ler_ultima_por_cnpj(
        caminho, "CNPJ_FUNDO_CLASSE", "DT_COMPTC"
    ).items():
        fundos[cnpj] = {
            "cnpj": cnpj,
            "nome": _texto(linha.get("DENOM_SOCIAL")),
            "data_ref": _texto(linha.get("DT_COMPTC")),
            "segmento": None,
            "vpa": _numero(linha.get("VL_QUOTA_CLASSE")),
            "patrimonio_liquido": _numero(linha.get("VL_PATRIM_LIQ")),
            "cotas": _numero(linha.get("QT_COTA_SUBSCR_CLASSE")),
            "cotistas": _numero(linha.get("NR_TOTAL_COTST_SUBSCR")) or None,
        }
    return fundos


def _diario_do_mes(ano: int, destino: Path) -> dict[str, dict[str, Any]]:
    """Diário do mês corrente; cai para o anterior se ainda não publicado."""

    for ano_mes in _meses_recentes():
        nome = f"inf_diario_fi_{ano_mes}.csv"
        caminho = destino / nome
        if caminho.exists() and time.time() - caminho.stat().st_mtime < VALIDADE_DIARIO_S:
            return ler_diario_fi(ano_mes, destino)
        try:
            _baixar_e_extrair(
                URL_DIARIO.format(ano_mes=ano_mes), destino / nome, (nome,)
            )
            return ler_diario_fi(ano_mes, destino)
        except Exception:  # noqa: BLE001 - tenta o mês anterior
            continue
    raise FileNotFoundError("sem diário de fundos publicado na CVM")


def _fiagro_do_mes(ano: int, destino: Path) -> dict[str, dict[str, Any]]:
    for ano_mes in _meses_recentes():
        nomes = (
            f"inf_mensal_fiagro_{ano_mes}.csv",
            f"inf_mensal_fiagro_subclasse_{ano_mes}.csv",
        )
        caminhos = [destino / nome for nome in nomes]
        if all(caminho.exists() for caminho in caminhos):
            return ler_fiagro(ano_mes, destino)
        try:
            _baixar_e_extrair(
                URL_FIAGRO.format(ano_mes=ano_mes), destino / nomes[0], nomes
            )
            return ler_fiagro(ano_mes, destino)
        except Exception:  # noqa: BLE001 - tenta o mês anterior
            continue
    raise FileNotFoundError("sem informe mensal de Fiagro publicado na CVM")


def _fip_do_ano(ano: int, destino: Path) -> dict[str, dict[str, Any]]:
    caminho = destino / f"inf_quadrimestral_fip_{ano}.csv"
    if caminho.exists() and time.time() - caminho.stat().st_mtime < VALIDADE_CACHE_S:
        return ler_fip(ano, destino)

    temporario = caminho.with_suffix(caminho.suffix + ".part")
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as cliente:
        with cliente.stream("GET", URL_FIP.format(ano=ano)) as resposta:
            resposta.raise_for_status()
            with temporario.open("wb") as arquivo:
                for bloco in resposta.iter_bytes():
                    arquivo.write(bloco)
    temporario.replace(caminho)
    return ler_fip(ano, destino)


def ler_fundos_alternativos(
    ano: int, cnpjs: Iterable[str], destino: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Dados de Fi-Infra, Fiagro e FIP-Infra que faltam no informe mensal de FII.

    Recebe os CNPJs que não apareceram no ``ler_fundos`` e devolve o primeiro
    valor de cota encontrado, na ordem diário de FI → Fiagro → FIP. Falha de
    uma fonte não impede as outras.
    """

    alvo = {str(c).strip() for c in cnpjs if str(c).strip()}
    diretorio = Path(destino) if destino is not None else DIRETORIO
    diretorio.mkdir(parents=True, exist_ok=True)

    fundos: dict[str, dict[str, Any]] = {}
    fontes: tuple[Callable[[int, Path], dict[str, dict[str, Any]]], ...] = (
        _diario_do_mes,
        _fiagro_do_mes,
        _fip_do_ano,
    )
    for carregar in fontes:
        faltando = alvo - fundos.keys()
        if not faltando:
            break
        try:
            encontrados = carregar(ano, diretorio)
        except Exception:  # noqa: BLE001 - a próxima fonte pode cobrir
            continue
        for cnpj in faltando:
            if cnpj in encontrados:
                fundos[cnpj] = encontrados[cnpj]
    return fundos
