#!/usr/bin/env bash
# 宿主依賴安裝（Ubuntu 22.04）
#
# 目標宿主現況：qemu 6.2 / libvirt 8.0.0 / virt-install / cloud-localds 已具備。
# 本腳本可重複執行，只補缺的部分。
set -euo pipefail

PKGS=(
  # 虛擬化
  qemu-kvm qemu-system-x86 qemu-utils
  libvirt-daemon-system libvirt-clients
  virtinst              # virt-install
  cloud-image-utils     # cloud-localds，產生 cloud-init seed

  # 網關
  nftables

  # 渲染與設定解析
  python3-yaml
  curl
)

apt-get update
apt-get install -y --no-install-recommends "${PKGS[@]}"

# virtiofsd：Ubuntu 22.04 的 apt 沒有獨立套件，
# 但 qemu-system-common 已在 /usr/lib/qemu/virtiofsd 提供（QEMU 6.2 內建 C 版）。
# libvirt 會自動從此路徑啟動它，不需要額外設定。
if [[ ! -x /usr/lib/qemu/virtiofsd ]]; then
  echo "ERROR: /usr/lib/qemu/virtiofsd 不存在，virtiofs 無法使用" >&2
  echo "       QEMU 7.0 之後已移除 C 版 virtiofsd，屆時需改用 Rust 版（crates.io/virtiofsd）" >&2
  exit 1
fi

systemctl enable --now libvirtd

echo "--- 版本確認 ---"
virsh --version
qemu-system-x86_64 --version | head -1
/usr/lib/qemu/virtiofsd --help >/dev/null 2>&1 && echo "virtiofsd: present"

echo "OK"
