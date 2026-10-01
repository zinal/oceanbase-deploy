#!/usr/bin/env bash
# Начальная установка кластера из трёх observer без OCP и OBD.
# По умолчанию только печатает команды (--print). Выполнение — --yes.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
PY="${LIB_DIR}/ob_expand.py"

usage() {
  cat <<'EOF'
Использование: ./scripts/23-bootstrap-cluster.sh --zone1 ADDR --zone2 ADDR --zone3 ADDR [опции]

На каждом адресе запускается bin/observer с общим списком Root Service.
Затем с первого узла выполняется ALTER SYSTEM BOOTSTRAP. OCP и OBD не вызываются.

  --zone1 ADDR          адрес узла zone1
  --zone2 ADDR          адрес узла zone2
  --zone3 ADDR          адрес узла zone3
  --ssh-user USER       один пользователь SSH на всех трёх узлах (obadmin)
  --ssh-key PATH
  --ssh-port PORT
  --appname NAME        имя кластера (obcluster)
  --cluster-id N
  --home PATH           каталог с bin/observer
  --data-dir PATH
  --redo-dir PATH       отдельный каталог Clog (симлинк data_dir/clog)
  --memory-limit SIZE
  --system-memory SIZE
  --datafile-size SIZE
  --log-disk-size SIZE
  --mysql-port PORT
  --rpc-port PORT
  --wipe                остановить свой observer и очистить data/redo на этих трёх узлах
  --print               показать команды (по умолчанию)
  --yes                 выполнить SSH и BOOTSTRAP

Пароль root@sys после старта пустой, пока его не задали. OB_ROOT_PASSWORD
нужен только если bootstrap повторяют на кластере, где пароль уже есть.
На каждом хосте заранее лежит один и тот же RPM: $HOME/bin/observer.
Диски под data_dir и redo_dir скрипт не размечает.
Повторный --wipe на уже работающем кластере стирает данные этих узлов.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

ZONE1=""
ZONE2=""
ZONE3=""
SSH_USER="obadmin"
SSH_KEY=""
SSH_PORT="22"
APPNAME="obcluster"
CLUSTER_ID="1"
HOME_PATH="/home/obadmin/observer"
DATA_DIR="/data/1"
REDO_DIR="/data/log1"
MEMORY_LIMIT="64G"
SYSTEM_MEMORY="16G"
DATAFILE_SIZE="192G"
LOG_DISK_SIZE="192G"
MYSQL_PORT="2881"
RPC_PORT="2882"
WIPE=0
MODE="print"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone1) ZONE1="$2"; shift 2 ;;
    --zone2) ZONE2="$2"; shift 2 ;;
    --zone3) ZONE3="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --appname) APPNAME="$2"; shift 2 ;;
    --cluster-id) CLUSTER_ID="$2"; shift 2 ;;
    --home) HOME_PATH="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --redo-dir) REDO_DIR="$2"; shift 2 ;;
    --memory-limit) MEMORY_LIMIT="$2"; shift 2 ;;
    --system-memory) SYSTEM_MEMORY="$2"; shift 2 ;;
    --datafile-size) DATAFILE_SIZE="$2"; shift 2 ;;
    --log-disk-size) LOG_DISK_SIZE="$2"; shift 2 ;;
    --mysql-port) MYSQL_PORT="$2"; shift 2 ;;
    --rpc-port) RPC_PORT="$2"; shift 2 ;;
    --wipe) WIPE=1; shift ;;
    --print) MODE="print"; shift ;;
    --yes) MODE="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "неизвестный аргумент: $1" ;;
  esac
done

[[ -n "$ZONE1" && -n "$ZONE2" && -n "$ZONE3" ]] || { usage >&2; die "нужны --zone1, --zone2 и --zone3"; }
[[ -f "$PY" ]] || die "нет $PY"

RS="${ZONE1},${ZONE2},${ZONE3}"
common=(
  --rs "$RS"
  --appname "$APPNAME" --cluster-id "$CLUSTER_ID"
  --home "$HOME_PATH" --data-dir "$DATA_DIR" --redo-dir "$REDO_DIR"
  --mysql-port "$MYSQL_PORT" --rpc-port "$RPC_PORT"
  --memory-limit "$MEMORY_LIMIT" --system-memory "$SYSTEM_MEMORY"
  --datafile-size "$DATAFILE_SIZE" --log-disk-size "$LOG_DISK_SIZE"
  --bootstrap
)
if [[ "$WIPE" -eq 1 ]]; then
  common+=(--wipe)
fi

remote1="$(python3 "$PY" observer-remote --ip "$ZONE1" --zone zone1 "${common[@]}")" || exit $?
remote2="$(python3 "$PY" observer-remote --ip "$ZONE2" --zone zone2 "${common[@]}")" || exit $?
remote3="$(python3 "$PY" observer-remote --ip "$ZONE3" --zone zone3 "${common[@]}")" || exit $?
sql="$(python3 "$PY" bootstrap-sql --zone1 "$ZONE1" --zone2 "$ZONE2" --zone3 "$ZONE3" --rpc-port "$RPC_PORT")" || exit $?

bare() {
  local ip="$1"
  ip="${ip#[}"
  ip="${ip%]}"
  printf '%s' "$ip"
}

BARE1="$(bare "$ZONE1")"
BARE2="$(bare "$ZONE2")"
BARE3="$(bare "$ZONE3")"

if [[ "$MODE" == "print" ]]; then
  echo "# удалённый скрипт zone1 ${SSH_USER}@${BARE1} (не выполнен)"
  printf '%s\n' "$remote1"
  echo "# удалённый скрипт zone2 ${SSH_USER}@${BARE2} (не выполнен)"
  printf '%s\n' "$remote2"
  echo "# удалённый скрипт zone3 ${SSH_USER}@${BARE3} (не выполнен)"
  printf '%s\n' "$remote3"
  echo "# SQL на ${BARE1}:${MYSQL_PORT}"
  echo "$sql"
  exit 0
fi

command -v ssh >/dev/null 2>&1 || die "нет ssh"
client="$(command -v obclient || command -v mysql || true)"
[[ -n "$client" ]] || die "нужен obclient или mysql на управляющей машине"

ssh_one() {
  local ip="$1" script="$2"
  local -a ssh_opts dest
  ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -p "$SSH_PORT")
  if [[ -n "$SSH_KEY" ]]; then
    ssh_opts+=(-i "$SSH_KEY")
  fi
  if [[ "$ip" == *:* ]]; then
    ssh_opts+=(-6)
    dest="${SSH_USER}@[${ip}]"
  else
    dest="${SSH_USER}@${ip}"
  fi
  echo "Запуск observer на ${dest}"
  printf '%s\n' "$script" | ssh "${ssh_opts[@]}" "$dest" bash -s
}

run_sql() {
  local host="$1" statement="$2"
  if [[ -n "${OB_ROOT_PASSWORD:-}" ]]; then
    MYSQL_PWD="${OB_ROOT_PASSWORD}" "$client" -h "$host" -P "$MYSQL_PORT" -uroot --connect-timeout=8 -e "$statement"
  else
    env -u MYSQL_PWD "$client" -h "$host" -P "$MYSQL_PORT" -uroot --connect-timeout=8 -e "$statement"
  fi
}

ssh_one "$BARE1" "$remote1"
ssh_one "$BARE2" "$remote2"
ssh_one "$BARE3" "$remote3"

echo "BOOTSTRAP через ${BARE1}"
set +e
out="$(run_sql "$BARE1" "$sql" 2>&1)"
rc=$?
set -e
printf '%s\n' "$out"
if [[ "$rc" -ne 0 ]]; then
  die "BOOTSTRAP не выполнен. Повтор по непустому clog — только --wipe, и только если кластер ещё не собран."
fi

echo "Ожидание ACTIVE на трёх узлах"
deadline=$((SECONDS + 900))
while (( SECONDS < deadline )); do
  rows="$(run_sql "$BARE1" "SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS" 2>/dev/null || true)"
  ok=0
  for ip in "$BARE1" "$BARE2" "$BARE3"; do
    if printf '%s\n' "$rows" | python3 "$PY" status-active --ip "$ip"; then
      ok=$((ok + 1))
    fi
  done
  if [[ "$ok" -eq 3 ]]; then
    echo "zone1 ${BARE1}, zone2 ${BARE2}, zone3 ${BARE3}: ACTIVE"
    echo "Дальше узлы добавляет ./scripts/21-expand-observer.sh, прокси — ./scripts/22-expand-obproxy.sh."
    exit 0
  fi
  sleep 5
done
die "не все три узла стали ACTIVE за 900с"
