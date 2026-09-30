#!/usr/bin/env bash
# Запустить ещё один OBProxy без OCP и OBD и напечатать строку для L4.
# В DBA_OB_SERVERS прокси не добавляется: членства у него нет.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
PY="${LIB_DIR}/ob_expand.py"

usage() {
  cat <<'EOF'
Использование: ./scripts/22-expand-obproxy.sh --ip ADDR --rs IP,IP,IP [опции]

  --ip ADDR             адрес нового obproxy
  --rs IP,IP,IP         observer, к которым прокси ходит за Root Service
  --name NAME           имя сервера в HAProxy (obp4)
  --ssh-user USER
  --ssh-key PATH
  --ssh-port PORT
  --appname NAME
  --home PATH           каталог с bin/obproxy
  --listen-port PORT    внешний SQL (2883)
  --mysql-port PORT     SQL-порт observer (2881)
  --print               показать команды (по умолчанию)
  --yes                 запустить процесс по SSH

Клиентов после старта нужно направить на этот адрес через VIP/HAProxy.
Скрипт печатает строку `server ... check`.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

IP=""
RS=""
NAME=""
SSH_USER="obadmin"
SSH_KEY=""
SSH_PORT="22"
APPNAME="obcluster"
HOME_PATH="/home/obadmin/obproxy"
LISTEN_PORT="2883"
MYSQL_PORT="2881"
MODE="print"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --rs) RS="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --appname) APPNAME="$2"; shift 2 ;;
    --home) HOME_PATH="$2"; shift 2 ;;
    --listen-port) LISTEN_PORT="$2"; shift 2 ;;
    --mysql-port) MYSQL_PORT="$2"; shift 2 ;;
    --print) MODE="print"; shift ;;
    --yes) MODE="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done

[[ -n "$IP" && -n "$RS" ]] || { usage >&2; die "нужны --ip и --rs"; }
[[ -n "$NAME" ]] || NAME="obp"
[[ -f "$PY" ]] || die "нет $PY"

py_args=(
  --ip "$IP" --rs "$RS" --appname "$APPNAME" --home "$HOME_PATH"
  --listen-port "$LISTEN_PORT" --mysql-port "$MYSQL_PORT"
)
remote="$(python3 "$PY" obproxy-remote "${py_args[@]}")" || exit $?
line="$(python3 "$PY" haproxy-line --ip "$IP" --name "$NAME" --listen-port "$LISTEN_PORT")" || exit $?

BARE_IP="${IP#[}"
BARE_IP="${BARE_IP%]}"

if [[ "$MODE" == "print" ]]; then
  echo "# удалённый скрипт на ${SSH_USER}@${BARE_IP} (не выполнен)"
  printf '%s\n' "$remote"
  echo "# строка для HAProxy / L4"
  echo "$line"
  exit 0
fi

command -v ssh >/dev/null 2>&1 || die "нет ssh"
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -p "$SSH_PORT")
if [[ -n "$SSH_KEY" ]]; then
  ssh_opts+=(-i "$SSH_KEY")
fi
if [[ "$BARE_IP" == *:* ]]; then
  ssh_opts+=(-6)
  dest="${SSH_USER}@[${BARE_IP}]"
else
  dest="${SSH_USER}@${BARE_IP}"
fi

echo "Запуск obproxy на ${dest}"
printf '%s\n' "$remote" | ssh "${ssh_opts[@]}" "$dest" bash -s
echo "Добавьте в backend L4:"
echo "$line"
