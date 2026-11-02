# claude-vm

為 Claude Code 準備的隔離執行環境。單台 Ubuntu VM，宿主提供運算與儲存，對外流量預設經透明代理。

設計目標是**可跨宿主複用**：換一台 Ubuntu 機器，clone repo、裝依賴、填設定、跑腳本，就能得到同樣的環境。沒有硬編碼的路徑、網段或節點名稱。

## 需求對照

| 需求 | 實作 |
|------|------|
| 對外流量走代理 | 宿主側 TPROXY 網關，`MATCH,UPSTREAM` 全量轉發 |
| 國內外自動分流 | 由**上游代理**決定，本倉庫不產生規則 |
| 代理對 VM 內使用者不可見 | 無客戶端、無環境變數、無 TUN 裝置 |
| 宿主目錄直接掛載 | virtiofs，含工具鏈與建置快取 |
| VM 可訪問宿主資料庫 | nftables DNAT + `route_localnet` |
| VM 不需被反向訪問 | 純 NAT 出向，無進向 port-forward |
| 宿主自身不啟用 TUN | 網關用獨立 Mihomo 實例，不干擾宿主既有代理 |

## 架構

```
                    區網
                     │
            ┌────────┴─────────────────────────────┐
            │  宿主                                 │
            │                                       │
            │  approx-client.service（既有，不動）  │
            │                                       │
            │  approx-gateway.service               │
            │    mihomo: tproxy 17893 / dns 17853   │
            │            ctrl 127.0.0.1:9099        │
            │    └─ MATCH,UPSTREAM ────────────────► 上游代理
            │                                       │
            │  virbr-approx  10.66.0.1/24           │
            │    ├─ nft TPROXY   對外流量           │
            │    ├─ nft DNAT     宿主服務           │
            │    └─ virtiofsd    目錄共享           │
            └────────┬──────────────────────────────┘
                     │
            ┌────────┴──────────────┐
            │  claude-vm            │
            │    閘道 = 10.66.0.1   │
            │    無代理設定         │
            └───────────────────────┘
```

VM 內任何程式（含 Claude Code）不需要知道代理存在。

## 快速開始

```bash
# 1. 宿主依賴
sudo ./host/install-deps.sh

# 2. 設定
bin/claude-vm init          # 產生 config.yaml
$EDITOR config.yaml         # 改路徑、節點、資源量
bin/claude-vm check         # 前置檢查，會驗證網段衝突與埠佔用

# 3. 部署
sudo bin/claude-vm up

# 4. 驗證
bin/claude-vm verify
```

`up` 依序執行 render → image → network → gateway → vm。每一步都可單獨執行。

首次開機 cloud-init 要下載套件並安裝 Claude Code，約 3–8 分鐘。

## 目錄

```
bin/claude-vm        唯一入口
lib/render.py        設定 + 訂閱 → 部署產物（純渲染，不改系統）
templates/           所有 @@VAR@@ 模板
host/                宿主依賴
docs/                設計、取捨、驗證
.state/rendered/     渲染產物（gitignored）
```

## 前置條件

- Ubuntu 22.04 / 24.04，`/dev/kvm` 可用
- libvirt + QEMU（`virtiofsd` 需存在；QEMU 6.2 內建於 `/usr/lib/qemu/virtiofsd`）
- **一個可用的上游代理位址**（`http://` 或 `socks5://`）。代理本身由你自行準備，本倉庫不管
- 一個 mihomo 核心二進位（`gateway.core_bin`，可用既有的或自官方 release 取得）
- 一組 SSH 公鑰（寫入客體）

上游代理必須**會做分流**。若它只是無規則的純轉發，國內站點也會繞出境外。

## 文件

- `docs/decisions.md` — 10 項設計決策與否決理由
- `docs/network.md` — 拓撲、封包路徑、三個必踩的坑
- `docs/storage.md` — virtiofs 與工具鏈共享
- `docs/host-services.md` — 端口轉發表與安全提醒
- `docs/verify.md` — 分階段驗證清單

## 狀態

**已端到端驗證**（2026-11-02，Ubuntu 22.04 / libvirt 8.0 / QEMU 6.2）：

乾淨 `destroy` + `up` 一次通過，`cloud-init status: done`，`errors: []`。
客體內 Claude Code 2.1.316、Go 1.26.6、Node v25.0.0 可用；
`go get` 經 goproxy.cn、`go build` 正常；出口 IP 為上游節點，國內站點直連。

逐項問題與處置見 `docs/bringup-log.md`。

技術驗證清單：

- 渲染產物語法：`nft -c`、XML、YAML 全數通過
- 渲染出的 mihomo 設定通過 `mihomo -t`（v1.19.29），初始配置 0ms（無 geodata 下載）
- 上游解析：`http://us-usa.approx.internal:17890` → `UPSTREAM` 條目，規則 `MATCH,UPSTREAM`
- `drop_quic: auto` 在 HTTP 上游下正確產生 `reject udp/443`
- `virtiofsd` 由 QEMU 6.2 內建提供
- `bin/claude-vm check` 全綠（含上游可連性）

**尚未實跑**：網路建立、VM 開機、virtiofs 掛載、cloud-init、端到端連通性。見 `docs/verify.md`。
