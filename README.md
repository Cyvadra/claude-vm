# claude-vm

為 Claude Code 準備的隔離執行環境。可跑多台 Ubuntu VM，宿主提供運算與儲存，對外流量經透明代理，每台有自己的出口。

設計目標是**可跨宿主複用**：換一台 Ubuntu 機器，clone repo、裝依賴、填設定、跑腳本，就能得到同樣的環境。

## 需求對照

| 需求 | 實作 |
|------|------|
| 對外流量走代理 | 宿主側 TPROXY 網關，`MATCH,UPSTREAM` 全量轉發 |
| 每台獨立出口 | 每台一個 mihomo 實例與上游，nft 按 `ip saddr` 分派 |
| 每台獨立時區 | `vms[].timezone`，省略時沿用 `guest.timezone`；建議與該台出口地區一致 |
| 國內外自動分流 | **不做**。上游 listener 是單節點直通、不含規則，全量走節點 |
| 代理對 VM 內使用者不可見 | 無客戶端、無環境變數、無 TUN 裝置 |
| 宿主目錄直接掛載 | virtiofs，工具鏈共用；建置快取留在各 VM 本地 |
| VM 可訪問宿主資料庫 | nftables DNAT + `route_localnet` |
| VM 不需被反向訪問 | 純 NAT 出向，無進向 port-forward |
| 每台 VM 身分獨立 | `~/.claude`、`~/.ssh`、git 身分皆不共用 |

## 架構

```
                    區網
                     │
            ┌────────┴─────────────────────────────┐
            │  宿主                                 │
            │                                       │
            │  approx-client（既有，不動）          │
            │                                       │
            │  approx-gateway@claude-vm    :17892/3/4 │
            │  approx-gateway@claude-vm-2  :17902/3/4 │
            │    └─ 各自 MATCH,UPSTREAM ────────────► 不同上游
            │                                       │
            │  virbr-approx  10.66.0.1/24           │
            │    ├─ nft saddr .107 → claude-vm 的埠 │
            │    ├─ nft saddr .101 → claude-vm-2 的埠│
            │    └─ virtiofsd    目錄共享           │
            └────────┬──────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        │ claude-vm               │ claude-vm-2
        │  閘道 = 10.66.0.1       │  閘道 = 10.66.0.1
        │  無代理設定             │  無代理設定
        └─────────────────────────┘
```

所有 VM 共用一個網段、一張 nft 表、一組 fwmark 與路由表。分派靠 `ip saddr`：TPROXY 與 REDIRECT 把封包改寫到各自的埠，核心按埠做 socket 查找，自然落到對應的 mihomo 實例。

VM 內任何程式（含 Claude Code）不需要知道代理存在。

## 快速開始

```bash
# 1. 宿主依賴
sudo ./host/install-deps.sh

# 2. 設定
bin/claude-vm init          # 產生 config.yaml
$EDITOR config.yaml         # 改路徑、節點、資源量、VM 清單
bin/claude-vm check         # 前置檢查，會驗證網段、埠位、資源總和

# 3. 部署
sudo bin/claude-vm up --all

# 4. 驗證
bin/claude-vm verify
```

`up` 依序執行 render → image → network → gateway → vm。每一步都可單獨執行。

首次開機 cloud-init 要下載套件並安裝 Claude Code，約 3–8 分鐘（視上游延遲）。

## 常用指令

```
bin/claude-vm ls                       列出 VM、狀態、IP、上游
bin/claude-vm status                   全域與各台狀態
bin/claude-vm verify [name...]         驗證（多台時會比對出口 IP 與 machineID 是否相異）
bin/claude-vm sshkey [name]            印出該台客體自己產生的 SSH 公鑰
bin/claude-vm destroy <name> [--purge] 只拆該台
bin/claude-vm teardown                 拆除全域層（網路、網關、nft）
```

只有一台 VM 時所有 `<name>` 都可省略。

登入 VM：

```bash
ssh -i .state/keys/<name> -o UserKnownHostsFile=.state/keys/<name>.known_hosts claude@<ip>
```

## 身分邊界

每台 VM 的 `machineID`、`userID`、`/etc/machine-id`、hostname、MAC、SSH host key、時區都不同。這些不是靠設定保證，是靠**不共用**：`~/.claude`、`~/.ssh`、git 身分都在各自磁碟上；時區由 `vms[].timezone` 逐台指定。

`lib/render.py` 的 `DENY_MOUNTS` 會拒絕把下列路徑掛進 VM（含其祖先目錄）：

```
~/.claude  ~/.config/claude  ~/.ssh  ~/.config/gh
~/.aws     ~/.config/gcloud  ~/.netrc  ~/.git-credentials
```

仍然共用的：`~/.kube`、`~/.docker`、`~/.nvm`、`~/programs`、`/usr/local/go`。每台的上游與出口則各自獨立。這些是明示的取捨，理由見 `docs/decisions.md` 的 D11/D12。

**共用目錄只保證並發讀取安全。** 兩個 virtiofsd 掛載同一個宿主目錄時快取不互通，並發寫入會出現 rename 失敗或找不到剛建立的檔案。因此 `GOMODCACHE`、`GOCACHE`、npm cache 與 `~/workspace` 都在各 VM 本地磁碟。見 D13。

## 目錄

```
bin/claude-vm        唯一入口
lib/render.py        設定 → 部署產物（純渲染，不改系統）
templates/           所有 @@VAR@@ 模板
host/                宿主依賴
docs/                設計、取捨、驗證、事故記錄
.state/rendered/     渲染產物（gitignored）
.state/keys/         每台 VM 的管理金鑰（gitignored）
```

## 文件

- `docs/decisions.md`　設計決策與被否決的替代方案
- `docs/network.md`　封包路徑與 nft 規則
- `docs/storage.md`　磁碟佈局與 virtiofs
- `docs/host-services.md`　宿主資料庫轉發
- `docs/verify.md`　驗證清單
- `docs/parity.md`　宿主與客體的環境對齊
- `docs/claude-identity.md`　Claude Code 的身分識別機制（靜態分析）
- `docs/multi-vm.md`　多台 VM 的設計、實作現況與驗收結果
- `docs/bringup-log.md`　部署事故記錄，16 個實際踩到的坑
