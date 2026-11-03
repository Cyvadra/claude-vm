# 儲存與目錄共享

## 掛載

| 宿主 | 客體 | 模式 | 備註 |
|------|------|------|------|
| `~/workspace`（或 `~/workspace-2`） | `/home/<user>/workspace` | 讀寫 | **per-VM**，每台一份 |
| `~/programs` | `/home/<user>/programs` | 讀寫 | 共用，不可並發寫入 |
| `/usr/local/go` | `/usr/local/go` | 讀寫（實際唯讀，見下） | 共用 |
| `~/.nvm` | `/home/<user>/.nvm` | 讀寫 | 共用，不可並發寫入 |
| `~/.kube` | `/home/<user>/.kube` | 讀寫 | 憑證，D12 的明示取捨 |
| `~/.docker` | `/home/<user>/.docker` | 讀寫 | 憑證，D12 的明示取捨 |

`GOMODCACHE`（`~/go/pkg/mod`）、`GOCACHE`（`~/.cache/go-build`）與 npm cache
**不在掛載清單**，留在各 VM 本地磁碟。實際清單由 `config.yaml` 的 `mounts` 決定。

## 工具鏈共享，快取本地

工具鏈（Go、Node、`~/programs`）共享，客體不必安裝 Go 或 Node，路徑與宿主一致。
但快取不共享：兩個 virtiofsd 掛載同一目錄時，彼此的 dentry/attr 快取不互通，
並發寫入會出現 rename 失敗或找不到暫存檔（D13）。

`/etc/profile.d/claude-vm-toolchain.sh` 仍設定相同路徑，只是這些目錄現在落在
各 VM 自己的磁碟：

```bash
# /etc/profile.d/claude-vm-toolchain.sh（由 cloud-init 寫入）
export GOROOT=/usr/local/go
export GOPATH=$HOME/go
export GOMODCACHE=$HOME/go/pkg/mod
export GOCACHE=$HOME/.cache/go-build
export PATH=$GOROOT/bin:$GOPATH/bin:$HOME/.local/bin:$PATH
export NVM_DIR=$HOME/.nvm
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
```

**權限靠 uid 一致解決**：`accessmode=passthrough` 讓客體 uid 直接對應宿主 uid。`config.yaml` 的 `guest.uid` 必須等於宿主檔案擁有者（本機為 1000）。

**`/usr/local/go` 的特殊情況**：它是 `root:root 755`，客體以 uid 1000 只能讀。即使 `read_only: false`，實際效果仍是唯讀——這是檔案權限的自然結果。

**寫入規則**：共用目錄只保證**並發讀取**安全；要寫入就必須獨佔。
`~/.nvm`、`~/programs` 不可從兩台 VM 同時寫入（例如同時 `nvm install`）。
`GOMODCACHE`/`GOCACHE`/npm cache 已改為本地，避免並發建置損壞。代價是每台
VM 首次建置要自行下載模組，約 1–3 GB。

## 機制：virtiofs

libvirt domain 需兩項設定：

```xml
<memoryBacking>
  <source type='memfd'/>
  <access mode='shared'/>
</memoryBacking>

<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs' queue='8'/>
  <source dir='/home/jason89757/workspace'/>
  <target dir='workspace'/>
</filesystem>
```

- `memfd` + `shared` 是 virtiofs 的硬性前提，缺少會直接啟動失敗
- `queue='8'` 給足平行佇列，metadata 密集操作（`git status`、`rg`）受益明顯
- `accessmode='passthrough'`：客體 uid 直接對應宿主 uid，不做映射。要求客體使用者是 uid 1000

客體 `/etc/fstab`：

```
workspace  /home/claude/workspace  virtiofs  defaults,_netdev,nofail,x-systemd.device-timeout=10  0 0
programs   /home/claude/programs   virtiofs  defaults,_netdev,nofail,x-systemd.device-timeout=10  0 0
```

`_netdev` 讓 systemd 等到 virtiofs 裝置就緒；`nofail` 避免掛載失敗卡住開機。

## virtiofsd

Ubuntu 22.04 的 apt **沒有**獨立的 `virtiofsd` 套件，但 `qemu-system-common` 在 `/usr/lib/qemu/virtiofsd` 提供了 C 版實作（QEMU 6.2 內建）。libvirt 會自動從此路徑啟動，無需額外設定。

> QEMU 7.0 起移除了 C 版 virtiofsd，改由獨立的 Rust 專案維護。若日後升級 QEMU，需要另外取得 Rust 版並確認 libvirt 的搜尋路徑。

## 待驗證的風險

| 風險 | 影響 | 驗證方式 |
|------|------|---------|
| 檔案鎖行為 | `go build`、資料庫、sqlite 在共享目錄上可能失敗 | 在客體 `~/workspace` 下跑 `go build ./...` |
| xattr 支援 | 部分工具（`setfacl`、SELinux label）失效 | `setfattr -n user.test -v 1` |
| `mmap` 語意 | 某些資料庫/編輯器行為異常 | 實測 |
| 大量小檔效能 | `rg` 全樹搜尋可能偏慢 | 對比宿主本地耗時 |
| 宿主端檔案權限 | `accessmode=passthrough` 下客體 uid 必須是 1000 | `id claude` |

## 為何不用其他方案

| 方案 | 否決理由 |
|------|---------|
| 9p | metadata 操作明顯慢，Claude Code 的工作負載正好是這類 |
| NFS | 需要額外服務、uid 映射麻煩，且增加宿主對外暴露面 |
| 複製目錄 | 35G，且失去雙向即時性 |
| SSHFS | 延遲高，不適合大量小檔讀寫 |
