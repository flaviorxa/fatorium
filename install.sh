#!/bin/sh
set -eu

REPO_COMPOSE="https://raw.githubusercontent.com/flaviorxa/fatorium/main/docker-compose.yml"
DIR="${FATORIUM_DIR:-$HOME/fatorium}"
PORTA=""

uso() {
  echo "uso: $0 [--porta N]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --porta)
      if [ $# -lt 2 ]; then
        uso
        exit 1
      fi
      PORTA="$2"
      shift 2
      ;;
    --porta=*)
      PORTA="${1#--porta=}"
      shift
      ;;
    -h|--help)
      echo "uso: $0 [--porta N]"
      exit 0
      ;;
    *)
      echo "argumento desconhecido: $1" >&2
      uso
      exit 1
      ;;
  esac
done

if [ -n "$PORTA" ]; then
  case "$PORTA" in
    *[!0-9]*|"")
      echo "erro: --porta espera um número inteiro" >&2
      exit 1
      ;;
  esac
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "erro: Docker com o plugin 'docker compose' não foi encontrado." >&2
  echo "Instale o Docker Engine e o plugin Compose v2 e tente novamente." >&2
  exit 1
fi

mkdir -p "$DIR"
cd "$DIR"

echo "Baixando docker-compose.yml..."
curl -fsSL "$REPO_COMPOSE" -o docker-compose.yml

if [ ! -f .env ]; then
  if command -v openssl >/dev/null 2>&1; then
    INVEST_SENHA="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)"
  else
    INVEST_SENHA="$(date +%s | sha256sum | cut -c1-32)"
  fi
  {
    printf 'INVEST_SENHA=%s\n' "$INVEST_SENHA"
    if [ -n "$PORTA" ]; then
      printf 'FATORIUM_PORTA=%s\n' "$PORTA"
    fi
  } > .env
  chmod 600 .env
  echo ".env criado com senha aleatória."
else
  echo ".env já existe; senha atual preservada."
  if [ -n "$PORTA" ]; then
    tmp_env="$(mktemp "${TMPDIR:-/tmp}/fatorium-env.XXXXXX")"
    awk -v porta="$PORTA" '
      BEGIN { atualizado = 0 }
      /^FATORIUM_PORTA=/ {
        if (!atualizado) {
          print "FATORIUM_PORTA=" porta
          atualizado = 1
        }
        next
      }
      { print }
      END {
        if (!atualizado) print "FATORIUM_PORTA=" porta
      }
    ' .env > "$tmp_env"
    cat "$tmp_env" > .env
    rm -f "$tmp_env"
    echo "Porta atualizada para ${PORTA} em .env."
  fi
fi

chmod 600 .env

docker compose up -d

PORTA_ENV="$(sed -n 's/^FATORIUM_PORTA=//p' .env 2>/dev/null | tail -n 1)"
PORTA_EXIBIR="${PORTA:-${PORTA_ENV:-8080}}"

echo
echo "FATORIUM iniciado."
echo "URL: http://localhost:${PORTA_EXIBIR}"
echo "Usuário: invest"
echo "Para ver a senha: grep INVEST_SENHA .env"
