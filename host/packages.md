# 宿主套件清單

## 必要

| 套件 | 提供 | 用途 |
|------|------|------|
| `qemu-kvm` `qemu-system-x86` `qemu-utils` | `qemu-system-x86_64`, `qemu-img` | 虛擬化與映像管理 |
| `libvirt-daemon-system` `libvirt-clients` | `libvirtd`, `virsh` | VM 生命週期 |
| `virtinst` | `virt-install` | 建立 VM（或直接用 `virsh define`） |
| `cloud-image-utils` | `cloud-localds` | 打包 cloud-init seed ISO |
| `nftables` | `nft` | TPROXY / DNAT 規則 |

## 已內建，不需安裝

| 元件 | 路徑 | 說明 |
|------|------|------|
| `virtiofsd` | `/usr/lib/qemu/virtiofsd` | QEMU 6.2 內建的 C 版實作。Ubuntu 22.04 的 apt 無獨立套件，libvirt 會自動偵測此路徑 |

> QEMU 7.0 起移除 C 版 virtiofsd，改由獨立的 Rust 專案維護。若日後升級 QEMU，需另外取得 Rust 版並確認 libvirt 的搜尋路徑。

## 刻意不安裝

| 套件 | 理由 |
|------|------|
| `libguestfs-tools` | 只有在需要離線修復客體映像時才需要，體積大 |
| `virt-manager` | GUI，本專案走命令列與 libvirt XML |
| `cockpit-machines` | 已在宿主安裝，僅供人工檢視，不作為部署路徑 |
