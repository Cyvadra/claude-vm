# 宿主與 VM 的環境對等

`~/workspace` 是共用的，同一份設定檔、同一份程式碼要在兩邊都能跑。
這份文件記錄「哪些已經對齊、哪些刻意不同、哪些尚未處理」。

## 已對齊

| 項目 | 機制 |
|------|------|
| Go 工具鏈 | `/usr/local/go` 共享；`GOROOT`/`GOPATH`/`GOMODCACHE`/`GOCACHE` 指向同一批目錄 |
| Go 環境設定 | 部署時讀宿主 `go env`，8 個鍵寫入客體 `~/.config/go/env`（含 `GOPROXY` 鏡像） |
| Node / nvm | `~/.nvm` 共享，`node -v` 兩邊一致 |
| npm registry | 部署時讀宿主 `npm config get registry`，寫入客體 `~/.npmrc` |
| git 身分 | 部署時讀宿主 `git config --global`，寫入客體 `~/.gitconfig` |
| 資料庫連線 | 客體內 `127.0.0.1:<port>` 經 nftables DNAT 到閘道，設定檔不需修改 |
| 時區 | `Asia/Shanghai` |

## 刻意不同

| 項目 | 原因 |
|------|------|
| 家目錄路徑 | `/home/claude` vs `/home/jason89757`，uid 相同所以共享目錄的權限正確 |
| 網路 | VM 走透明代理，出口 IP 不同 |
| 核心與系統套件 | VM 是獨立系統，版本可能不同 |

## 尚未處理

| 項目 | 影響 | 建議 |
|------|------|------|
| **git 認證** | 無法 clone/push 私有 repo（`could not read Username`） | 見下 |
| `gh` / `docker` / `kubectl` / `conda` / `aws` | 客體未安裝 | 依實際需求再加 |
| `~/.config/gh`、`~/.aws`、`~/.kube` | 未共享 | 依實際需求再掛載 |
| 宿主 `/etc/hosts` 的 `*.approx.internal` | 客體沒有這 24 條 | 目前無需；若要用到再注入 |

## git 認證的三個選項

宿主用 SSH remote（`git@github.com:lifesdk/...`），`~/.ssh/id_rsa` **沒有 passphrase**。

1. **掛載 `~/.ssh`（唯讀）** — 最省事，立刻可用。
   代價：VM 內任何程式都能讀到宿主私鑰。
2. **在客體產生專用金鑰，加到 GitHub** — 隔離最好。
   代價：多一步手動操作；一把金鑰只給這個 VM。
3. **用 GitHub PAT + `credential.helper store`** — 只給 repo 權限，可隨時撤銷。
   代價：token 以明文存在客體 `~/.git-credentials`。

目前未做任何選擇，維持無認證狀態。
