#!/usr/bin/env bash
# Добавить один observer в уже работающий кластер без OCP и OBD.
# По умолчанию только печатает команды (--print). Выполнение — --yes.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
PY="${LIB_DIR}/ob_expand.py"

usage() {
  cat <<'EOF'
Использование: ./scripts/21-expand-observer.sh --ip ADDR --zone ZONE --rs IP,IP,IP [опции]

Новый узел запускается бинарём observer (-6 и [ipv6]:rpc:sql для IPv6).
В кластер он входит через ALTER SYSTEM ADD SERVER. OCP и OBD не вызываются.

  --ip ADDR             стабильный адрес нового узла
  --zone ZONE           zone1 / zone2 / zone3 (уже существующая zone)
  --rs IP,IP,IP         уже работающие observer
  --seed ADDR           куда слать SQL (по умолчанию первый адрес из --rs)
  --ssh-user USER       пользователь SSH на новом узле (obadmin)
  --ssh-key PATH        приватный ключ
  --ssh-port PORT       порт SSH (22)
  --appname NAME        имя кластера (obcluster)
  --cluster-id N        cluster_id (1)
  --home PATH           каталог с bin/observer
  --data-dir PATH       -d, SSTable
  --redo-dir PATH       отдельный каталог Clog (симлинк data_dir/clog)
  --memory-limit SIZE   например 64G или 800G
  --system-memory SIZE
  --datafile-size SIZE
  --log-disk-size SIZE
  --mysql-port PORT
  --rpc-port PORT
  --wipe                остановить свой observer и очистить data/redo только на этом узле
  --print               показать удалённый скрипт и SQL (по умолчанию)
  --yes                 выполнить SSH и ADD SERVER

Пароль root@sys: переменная OB_ROOT_PASSWORD. Пустая — подключение без пароля.
На новом хосте заранее лежит тот же RPM, что на кластере: $HOME/bin/observer.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

IP=""
ZONE=""
RS=""
SEED=""
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
    --ip) IP="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --rs) RS="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
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

[[ -n "$IP" && -n "$ZONE" && -n "$RS" ]] || { usage >&2; die "нужны --ip, --zone и --rs"; }
[[ -f "$PY" ]] || die "нет $PY"

py_args=(
  --ip "$IP" --zone "$ZONE" --rs "$RS"
  --appname "$APPNAME" --cluster-id "$CLUSTER_ID"
  --home "$HOME_PATH" --data-dir "$DATA_DIR" --redo-dir "$REDO_DIR"
  --mysql-port "$MYSQL_PORT" --rpc-port "$RPC_PORT"
  --memory-limit "$MEMORY_LIMIT" --system-memory "$SYSTEM_MEMORY"
  --datafile-size "$DATAFILE_SIZE" --log-disk-size "$LOG_DISK_SIZE"
)
if [[ "$WIPE" -eq 1 ]]; then
  py_args+=(--wipe)
fi

remote="$(python3 "$PY" observer-remote "${py_args[@]}")" || exit $?
sql="$(python3 "$PY" add-server-sql "${py_args[@]}")" || exit $?

if [[ -z "$SEED" ]]; then
  SEED="${RS%%,*}"
fi
SEED="${SEED#[}"
SEED="${SEED%]}"
BARE_IP="${IP#[}"
BARE_IP="${BARE_IP%]}"

if [[ "$MODE" == "print" ]]; then
  echo "# удалённый скрипт на ${SSH_USER}@${BARE_IP} (не выполнен)"
  printf '%s\n' "$remote"
  echo "# SQL на ${SEED}:${MYSQL_PORT}"
  echo "$sql"
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

echo "Запуск observer на ${dest}"
printf '%s\n' "$remote" | ssh "${ssh_opts[@]}" "$dest" bash -s

client="$(command -v obclient || command -v mysql || true)"
[[ -n "$client" ]] || die "нужен obclient или mysql на управляющей машине"

run_sql() {
  local host="$1" statement="$2"
  if [[ -n "${OB_ROOT_PASSWORD:-}" ]]; then
    MYSQL_PWD="${OB_ROOT_PASSWORD}" "$client" -h "$host" -P "$MYSQL_PORT" -uroot --connect-timeout=8 -e "$statement"
  else
    env -u MYSQL_PWD "$client" -h "$host" -P "$MYSQL_PORT" -uroot --connect-timeout=8 -e "$statement"
  fi
}

echo "ADD SERVER через ${SEED}"
set +e
out="$(run_sql "$SEED" "$sql" 2>&1)"
rc=$?
set -e
printf '%s\n' "$out"
if [[ "$rc" -ne 0 ]]; then
  if grep -q '4179' <<<"$out"; then
    die "ERROR 4179: узел не пустой. Повторите с --wipe (очистится только ${BARE_IP})"
  fi
  if ! grep -qiE 'already exist|duplicate' <<<"$out"; then
    die "ADD SERVER не выполнен"
  fi
fi

echo "Ожидание ACTIVE"
deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  rows="$(run_sql "$SEED" "SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS" 2>/dev/null || true)"
  if printf '%s\n' "$rows" | python3 "$PY" status-active --ip "$BARE_IP"; then
    echo "${BARE_IP} ACTIVE"
    echo "UNIT_NUM сам не вырастет. Если новый узел должен нести unit, это отдельный ALTER RESOURCE."
    exit 0
  fi
  sleep 5
done
die "${BARE_IP} не стал ACTIVE за 300с"
