#!/usr/bin/env bash
# Подготовка ВМ для OceanBase Cloud Platform (OCP): Java, clockdiff, каталоги.
# Запускается на удалённом хосте через prepare-servers / deploy-ocp.

set -euo pipefail

if ! declare -F apt_get >/dev/null 2>&1; then
  if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    # shellcheck source=apt-retry.sh
    source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/apt-retry.sh"
  else
    echo "ERROR: apt_get не определён — prepend scripts/lib/apt-retry.sh" >&2
    exit 1
  fi
fi

DEPLOY_USER="${DEPLOY_USER:-}"
OCP_HOME="${OCP_HOME:-/home/obadmin/ocp}"
OCP_SOFT_DIR="${OCP_SOFT_DIR:-/ocp-data/software}"
OCP_LOG_DIR="${OCP_LOG_DIR:-/ocp-data/logs}"
JAVA_MIN_MAJOR="${JAVA_MIN_MAJOR:-8}"

require_root() {
  [[ "$(id -u)" -eq 0 ]] || {
    echo "ERROR: prepare-ocp-host.sh должен выполняться от root" >&2
    exit 1
  }
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt_get update -qq
    apt_get install -y -qq ca-certificates curl wget iputils-clockdiff libcap2-bin
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q java-11-openjdk-headless iputils libcap
  else
    echo "WARN: неизвестный пакетный менеджер — установите Java 8+ и clockdiff вручную" >&2
  fi
}

install_java() {
  if [[ -x /usr/bin/java ]]; then
    local ver
    ver="$(/usr/bin/java -version 2>&1 | head -1 || true)"
    if echo "${ver}" | grep -qE 'version "1\.[89]\.|version "[0-9]+'; then
      echo "Java уже установлена: ${ver}"
      return 0
    fi
  fi

  if command -v apt-get >/dev/null 2>&1; then
    apt_get install -y -qq openjdk-11-jdk-headless
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q java-11-openjdk-headless
  fi

  [[ -x /usr/bin/java ]] || {
    echo "ERROR: Java не найдена в /usr/bin/java (требование OBD для OCP)" >&2
    exit 1
  }

  /usr/bin/java -version
}

is_elf() {
  local path="$1" magic
  [[ -f "${path}" ]] || return 1
  magic="$(head -c 4 "${path}" 2>/dev/null || true)"
  [[ "${magic}" == $'\x7fELF' ]]
}

install_clockdiff() {
  if ! command -v clockdiff >/dev/null 2>&1 \
    && ! is_elf /usr/sbin/clockdiff \
    && ! is_elf /usr/bin/clockdiff \
    && ! is_elf /usr/lib/oceanbase/clockdiff.real; then
    echo "ERROR: clockdiff не установлен (пакет iputils-clockdiff / iputils)" >&2
    exit 1
  fi

  # OCP JVM вызывает `clockdiff <ip>` (mode 0, ICMP TIMESTAMP) без sudo.
  # Ubuntu кладёт ELF в /usr/sbin; PATH ocp-server обычно /usr/sbin:/usr/bin —
  # wrapper только в /usr/bin OCP не видит (лог: args=[ip] без -o, exit 1).
  # CAP_NET_RAW снимает Operation not permitted, но Yandex Cloud режет и
  # ICMP TIMESTAMP, и часто IP timestamps (`-o`). Wrapper всё равно ставим;
  # takeover ещё требует ocp.host.check.clock-diff.enable=false в UI/API.
  local real="/usr/lib/oceanbase/clockdiff.real" wrap="/usr/lib/oceanbase/clockdiff.wrap"
  local src="" cand dest

  # После прошлого прогона /usr/bin и /usr/sbin — скрипты. ELF только в real.
  for cand in "${real}" /usr/sbin/clockdiff /usr/bin/clockdiff /bin/clockdiff; do
    if is_elf "${cand}"; then
      src="${cand}"
      break
    fi
  done
  [[ -n "${src}" ]] || {
    echo "ERROR: нет ELF clockdiff (sbin/bin/real)" >&2
    exit 1
  }

  install -d -m 0755 /usr/lib/oceanbase
  if [[ "${src}" -ef "${real}" ]]; then
    echo "clockdiff ELF already ${real}"
  else
    install -m 0755 "${src}" "${real}"
    echo "clockdiff ELF ${src} → ${real}"
  fi

  if ! command -v setcap >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      apt_get install -y -qq libcap2-bin
    elif command -v yum >/dev/null 2>&1; then
      yum install -y -q libcap
    fi
  fi
  if command -v setcap >/dev/null 2>&1; then
    setcap cap_net_raw,cap_sys_nice+ep "${real}" || {
      echo "WARN: setcap ${real} не применился" >&2
    }
    getcap "${real}" || true
  else
    echo "WARN: нет setcap — установите libcap2-bin" >&2
  fi

  # Не setcap на скрипт: capability нужна ELF, которую exec'ает wrapper.
  cat > "${wrap}" <<'WRAP'
#!/bin/sh
REAL=/usr/lib/oceanbase/clockdiff.real
need_o=1
for a in "$@"; do
  case "$a" in
    -o|-o1) need_o=0 ;;
  esac
done
if [ "$need_o" = 1 ]; then
  exec "$REAL" -o "$@"
fi
exec "$REAL" "$@"
WRAP
  chmod 0755 "${wrap}"
  for dest in /usr/bin/clockdiff /usr/sbin/clockdiff; do
    install -m 0755 "${wrap}" "${dest}"
    echo "clockdiff wrapper ${dest} → ${real} -o (если нет -o/-o1)"
  done

  if [[ -n "${DEPLOY_USER}" ]]; then
    if sudo -u "${DEPLOY_USER}" test -x /usr/sbin/clockdiff && sudo -u "${DEPLOY_USER}" test -x "${real}"; then
      echo "clockdiff доступен ${DEPLOY_USER}: /usr/sbin + /usr/bin → ${real}"
    else
      echo "WARN: ${DEPLOY_USER} не может выполнить wrapper / ${real}" >&2
    fi
    local probe="${CLOCKDIFF_TEST_IP:-127.0.0.1}"
    if sudo -u "${DEPLOY_USER}" /usr/sbin/clockdiff "${probe}"; then
      echo "clockdiff wrapper ok ${DEPLOY_USER} /usr/sbin → ${probe} (как OCP: без -o в argv)"
    else
      echo "WARN: clockdiff wrapper к ${probe} exit $? — YC режет ICMP/IP timestamp; выключите ocp.host.check.clock-diff.enable" >&2
    fi
  fi
}

ensure_directories() {
  local user="$1" dir
  for dir in "${OCP_HOME}" "${OCP_SOFT_DIR}" "${OCP_LOG_DIR}"; do
    [[ -n "${dir}" ]] || continue
    install -d -o "${user}" -g "${user}" -m 0755 "${dir}"
    # test -w принимает ровно один путь; несколько аргументов → «extra argument».
    sudo -u "${user}" test -w "${dir}" || {
      echo "ERROR: ${dir} недоступен для записи пользователю ${user}" >&2
      exit 1
    }
  done
}

require_root
[[ -n "${DEPLOY_USER}" ]] || {
  echo "ERROR: DEPLOY_USER не задан" >&2
  exit 1
}

if [[ "${CLOCKDIFF_ONLY:-}" == "true" ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    apt_get update -qq
    apt_get install -y -qq iputils-clockdiff libcap2-bin
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q iputils libcap
  fi
  install_clockdiff
  echo "OCP clockdiff ready for ${DEPLOY_USER}"
  exit 0
fi

install_packages
install_java
install_clockdiff
ensure_directories "${DEPLOY_USER}"

echo "OCP host preparation complete for ${DEPLOY_USER}"
