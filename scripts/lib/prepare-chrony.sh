#!/usr/bin/env bash
# Chrony на узле OceanBase: синхронизация часов кластера (дока: offset < 2s, выборы < 100ms).
# Запускается на удалённом хосте через 02-prepare-servers.sh / 02-prepare-ocp.sh.
#
# NTP_SERVERS — необязательный список серверов через пробел (yandex_cloud.ntp_servers).
# Если пусто: NTP из DHCP (YC option 42) + серверы из документации Yandex Cloud.

set -euo pipefail

NTP_SERVERS="${NTP_SERVERS:-}"

# Рекомендованные NTP для Compute Cloud:
# https://yandex.cloud/en/docs/compute/tutorials/ntp
DEFAULT_NTP_SERVERS="0.ru.pool.ntp.org 1.ru.pool.ntp.org ntp.ix.ru ntp2.vniiftri.ru"

require_root() {
  [[ "$(id -u)" -eq 0 ]] || {
    echo "ERROR: prepare-chrony.sh должен выполняться от root" >&2
    exit 1
  }
}

chrony_service_name() {
  if systemctl cat chrony.service >/dev/null 2>&1; then
    echo chrony
  else
    echo chronyd
  fi
}

install_chrony() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q chrony
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q chrony
  else
    echo "ERROR: неизвестный пакетный менеджер — установите chrony вручную" >&2
    exit 1
  fi
  command -v chronyd >/dev/null 2>&1 || command -v chrony >/dev/null 2>&1 || {
    echo "ERROR: пакет chrony установлен, но бинарник chronyd не найден" >&2
    exit 1
  }
}

stop_competing_ntp() {
  local svc
  for svc in systemd-timesyncd ntp ntpsec ntpd; do
    systemctl disable --now "${svc}.service" >/dev/null 2>&1 || true
  done
  systemctl mask systemd-timesyncd.service >/dev/null 2>&1 || true
}

dhcp_ntp_servers() {
  local ntp="" iface name lease
  if command -v netplan >/dev/null 2>&1; then
    for iface in /sys/class/net/*; do
      name="$(basename "${iface}")"
      [[ "${name}" == lo ]] && continue
      ntp+=" $(netplan ip leases "${name}" 2>/dev/null | awk -F= '/^NTP=/ {print $2}')"
    done
  fi
  if [[ -d /run/systemd/netif/leases ]]; then
    for lease in /run/systemd/netif/leases/*; do
      [[ -f "${lease}" ]] || continue
      ntp+=" $(awk -F= '/^NTP=/ {print $2}' "${lease}")"
    done
  fi
  if [[ -d /var/lib/dhcp ]]; then
    ntp+=" $(awk '/ntp-servers/ {gsub(/;/, "", $3); print $3}' /var/lib/dhcp/*.leases 2>/dev/null || true)"
  fi
  echo "${ntp}" | tr -s '[:space:]' '\n' | awk 'NF && !seen[$0]++'
}

unique_lines() {
  awk 'NF && !seen[$0]++'
}

write_ntp_sources() {
  local -a dhcp_list=() extra=() defaults=()
  local tok dest sourcedir conf

  mapfile -t dhcp_list < <(dhcp_ntp_servers)
  # shellcheck disable=SC2086
  mapfile -t extra < <(printf '%s\n' ${NTP_SERVERS} | unique_lines)
  # shellcheck disable=SC2086
  mapfile -t defaults < <(printf '%s\n' ${DEFAULT_NTP_SERVERS} | unique_lines)

  if [[ -d /etc/chrony ]]; then
    mkdir -p /etc/chrony/sources.d
    dest=/etc/chrony/sources.d/oceanbase-ntp.sources
    sourcedir=/etc/chrony/sources.d
  elif [[ -f /etc/chrony.conf ]]; then
    dest=/etc/chrony.conf
    sourcedir=""
  else
    echo "ERROR: не найден /etc/chrony.conf — пакет chrony установлен некорректно" >&2
    exit 1
  fi

  local -a lines=()
  lines+=("# Сгенерировано scripts/lib/prepare-chrony.sh (OceanBase + Yandex Cloud).")
  for tok in "${dhcp_list[@]}"; do
    [[ -n "${tok}" ]] || continue
    lines+=("server ${tok} iburst prefer")
  done
  for tok in "${extra[@]}"; do
    [[ -n "${tok}" ]] || continue
    lines+=("server ${tok} iburst")
  done
  if ((${#dhcp_list[@]} == 0)) && ((${#extra[@]} == 0)); then
    for tok in "${defaults[@]}"; do
      [[ -n "${tok}" ]] || continue
      lines+=("server ${tok} iburst")
    done
  fi

  if [[ -n "${sourcedir}" ]]; then
    printf '%s\n' "${lines[@]}" >"${dest}"
    if [[ -f /etc/chrony/chrony.conf ]] && ! grep -qE '^[[:space:]]*sourcedir[[:space:]]+/etc/chrony/sources.d' /etc/chrony/chrony.conf; then
      echo "sourcedir ${sourcedir}" >>/etc/chrony/chrony.conf
    fi
  else
    # RHEL: не затираем chrony.conf, дописываем отсутствующие server.
    local line
    for line in "${lines[@]}"; do
      [[ "${line}" == \#* ]] && continue
      grep -qF "${line}" /etc/chrony.conf 2>/dev/null || echo "${line}" >>/etc/chrony.conf
    done
    dest=/etc/chrony.conf
  fi

  conf=""
  if [[ -f /etc/chrony/chrony.conf ]]; then
    conf=/etc/chrony/chrony.conf
  elif [[ -f /etc/chrony.conf ]]; then
    conf=/etc/chrony.conf
  fi
  if [[ -n "${conf}" ]]; then
    # Последняя директива makestep в файле побеждает; на Ubuntu пакетный 1.0 3 идёт после confdir.
    if grep -qE '^[[:space:]]*makestep[[:space:]]' "${conf}"; then
      sed -i -E 's/^[[:space:]]*makestep[[:space:]].*/makestep 1.0 -1/' "${conf}"
    else
      echo "makestep 1.0 -1" >>"${conf}"
    fi
  fi

  echo "chrony NTP sources:"
  printf '%s\n' "${lines[@]}"
}

enable_chrony() {
  local svc
  svc="$(chrony_service_name)"
  systemctl enable --now "${svc}.service"
  # Форсировать шаг сразу после старта (первые выборки NTP могут занять секунды).
  sleep 2
  chronyc makestep >/dev/null 2>&1 || chronyc -a makestep >/dev/null 2>&1 || true
  if chronyc tracking >/dev/null 2>&1; then
    echo "chrony tracking:"
    chronyc tracking || true
    echo "chrony sources:"
    chronyc sources || true
  else
    echo "WARN: chrony запущен, но tracking пока недоступен" >&2
  fi
}

require_root
install_chrony
stop_competing_ntp
write_ntp_sources
enable_chrony
echo "chrony установлен и запущен"
