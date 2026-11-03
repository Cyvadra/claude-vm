# 宿主與 VM 的環境對等

`~/workspace` 是共用的，同一份設定檔、同一份程式碼要在兩邊都能跑。
這份文件記錄「哪些已經對齊、哪些刻意不同、哪些尚未處理」。

## 已對齊

| 項目 | 機制 |
|------|------|
| Go 工具鏈 | `/usr/local/go` 共享；`GOROOT`/`GOPATH` 路徑一致，但 `GOMODCACHE`/`GOCACHE` 在**各 VM 本地磁碟**（D13） |
| Go 環境設定 | 部署時讀宿主 `go env`，8 個鍵寫入客體 `~/.config/go/env`（含 `GOPROXY` 鏡像） |
| Node / nvm | `~/.nvm` 共享，`node -v` 兩邊一致 |
| npm registry | 部署時讀宿主 `npm config get registry`，寫入客體 `~/.npmrc` |
| git 身分 | **不沿用宿主**：每台生成 `<name>` / `<name>@claude-vm.local` |
| 資料庫連線 | 客體內 `127.0.0.1:<port>` 經 nftables DNAT 到閘道，設定檔不需修改 |
| conda 設定 | 部署時讀宿主 `~/.condarc`（清華鏡像），寫入客體 |
| SSH 金鑰 | **不共用**。宿主用 `.state/keys/<name>` 管理；客體首次開機自產 GitHub 金鑰 |
| gh 認證 | **不共用**，需要時在客體內 `gh auth login` |
| kubectl 設定 | `~/.kube` 共享 |
| docker 設定 | `~/.docker` 共享 |
| 時區 | `vms[].timezone`，省略時取 `guest.timezone`。建議與該台出口 IP 的地理位置一致 |

## 刻意不同

| 項目 | 原因 |
|------|------|
| 家目錄路徑 | `/home/claude` vs `/home/jason89757`，uid 相同所以共享目錄的權限正確 |
| 網路 | VM 走透明代理，出口 IP 不同 |
| 核心與系統套件 | VM 是獨立系統，版本可能不同 |

## 尚未處理

| 項目 | 影響 | 建議 |
|------|------|------|
| 宿主 `/etc/hosts` 的 `*.approx.internal` | 客體沒有這 24 條 | 目前無需；若要用到再注入 |
| 宿主的 conda 環境 | 宿主 anaconda3 在 `/mnt/hdd`、160G，客體裝獨立 Miniconda | 需要既有 env 時另外掛載該路徑 |
| docker socket | **刻意不掛載** | 見下 |
| AWS CLI | 宿主本身沒裝 | 無需對齊 |
| gcloud | 宿主有，客體未裝 | 依需求再加 |

## git 認證：客體自行產生金鑰

`~/.ssh` 不再掛載。宿主→客體的管理金鑰由 cloud-init 注入（`.state/keys/<name>`）；
客體首次開機自行 `ssh-keygen -t ed25519` 產生對外金鑰，用
`bin/claude-vm sshkey <name>` 印出公鑰後貼到 GitHub。

`~/.config/gh`、`~/.aws`、`~/.config/gcloud`、`~/.netrc`、`~/.git-credentials`
同樣不掛載；需要時在 VM 內各自登入。`lib/render.py` 的 `DENY_MOUNTS` 會強制拒絕
把這些路徑（或其祖先目錄）掛進 VM。

代價是每台 VM 第一次使用 GitHub 前要手動貼一次公鑰；換來的是宿主私鑰不再暴露給 VM。

## docker：只裝 CLI 與 daemon，不掛 socket

客體安裝了 `docker.io`，有自己的 daemon，`docker` 與 `docker compose` 可用。

**沒有**掛載宿主 `/var/run/docker.sock`。掛了等於把宿主 root 權限交給 VM——
可以起特權容器、掛載宿主根目錄，隔離完全消失。這與共享 SSH 金鑰不是同一個量級。
若日後確實需要操作宿主容器，再單獨評估。
