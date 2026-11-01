#!/usr/bin/env bash
# The old initializer changed boot/network settings implicitly; use explicit menus.

onepve_index_main() {
  local root
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || return
  printf 'onePVE：先选择03准备显卡，再选择08创建 Agent 工作机。\n'
  bash "$root/menu.sh" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  onepve_index_main "$@"
fi
