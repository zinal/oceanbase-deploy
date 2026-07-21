#!/usr/bin/env bash
# Подготовка ВМ для OceanBase Cloud Platform (OCP): Java, clockdiff, каталоги.
# Запускается на удалённом хосте через prepare-servers / deploy-ocp.

set -euo pipefail

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
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl wget iputils-clockdiff
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q java-11-openjdk-headless iputils
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
    apt-get install -y -qq openjdk-11-jdk-headless
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q java-11-openjdk-headless
  fi

  [[ -x /usr/bin/java ]] || {
    echo "ERROR: Java не найдена в /usr/bin/java (требование OBD для OCP)" >&2
    exit 1
  }

  /usr/bin/java -version
}

install_clockdiff() {
  command -v clockdiff >/dev/null 2>&1 || {
    echo "ERROR: clockdiff не установлен (пакет iputils-clockdiff)" >&2
    exit 1
  }
}

ensure_directories() {
  local user="$1"
  install -d -o "${user}" -g "${user}" -m 0755 "${OCP_HOME}" "${OCP_SOFT_DIR}" "${OCP_LOG_DIR}"
  sudo -u "${user}" test -w "${OCP_HOME}" "${OCP_SOFT_DIR}" "${OCP_LOG_DIR}"
}

require_root
[[ -n "${DEPLOY_USER}" ]] || {
  echo "ERROR: DEPLOY_USER не задан" >&2
  exit 1
}

install_packages
install_java
install_clockdiff
ensure_directories "${DEPLOY_USER}"

echo "OCP host preparation complete for ${DEPLOY_USER}"
