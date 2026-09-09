#!/usr/bin/env bash
# Wrapper /usr/bin/clockdiff must add -o unless already present (OCP mode 0).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREPARE="${ROOT}/scripts/lib/prepare-ocp-host.sh"

grep -q '/usr/lib/oceanbase/clockdiff.real' "${PREPARE}"
grep -q 'exec "$REAL" -o "$@"' "${PREPARE}"
grep -q 'is_elf' "${PREPARE}"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
cat > "${tmp}/real" <<'EOF'
#!/bin/sh
printf 'ARGS:%s\n' "$*"
EOF
chmod 0755 "${tmp}/real"

# Same logic as prepare-ocp-host.sh WRAP heredoc, REAL overridden.
cat > "${tmp}/wrap" <<EOF
#!/bin/sh
REAL=${tmp}/real
need_o=1
for a in "\$@"; do
  case "\$a" in
    -o|-o1) need_o=0 ;;
  esac
done
if [ "\$need_o" = 1 ]; then
  exec "\$REAL" -o "\$@"
fi
exec "\$REAL" "\$@"
EOF
chmod 0755 "${tmp}/wrap"

out="$("${tmp}/wrap" 10.130.0.21)"
[[ "${out}" == "ARGS:-o 10.130.0.21" ]] || { echo "FAIL mode0: ${out}"; exit 1; }

out="$("${tmp}/wrap" -o 10.130.0.21)"
[[ "${out}" == "ARGS:-o 10.130.0.21" ]] || { echo "FAIL mode1: ${out}"; exit 1; }

out="$("${tmp}/wrap" -o1 10.130.0.21)"
[[ "${out}" == "ARGS:-o1 10.130.0.21" ]] || { echo "FAIL mode2: ${out}"; exit 1; }

echo "OK test-clockdiff-wrapper"
