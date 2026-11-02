# 設計決策

記錄已定案的選擇與被否決的替代方案，避免日後重複討論。

## D1　攔截點：TPROXY，而非 TUN

**選擇**：宿主側 nftables `PREROUTING` + `tproxy`，mihomo `tproxy-port`。

**理由**：TUN 模式（`auto-route` / `auto-redirect` / `strict-route`）作用於**本機產生**的流量，走 `OUTPUT`。VM 封包從 `virbr-approx` 進來，走 `PREROUTING → FORWARD → POSTROUTING`，永遠不碰 `OUTPUT`；`include-uid` 比對 socket 擁有者，轉發封包沒有 socket。所以 TUN + uid 過濾對 VM 無效，不是參數可調的問題。

**否決的替代**：客體內安裝代理客戶端（違反「不可見」，且每台 VM 都要維護）；`REDIRECT`/`redir-port`（只支援 TCP，且丟失原始目的地）。

## D2　VM 網路：獨立 NAT 網段，不橋接區網

**選擇**：`virbr-approx` / `10.66.0.0/24`，宿主為閘道，`forward mode='nat'`。

**理由**：透明代理要求宿主是 VM 的預設閘道。一旦 VM 直接橋接到宿主所在區網，封包不經宿主，沒有任何攔截點。

**代價**：VM 無法取得區網 IP，區網也無法主動訪問 VM。已確認可接受。

## D3　網關是獨立實例，不與宿主共用代理

**選擇**：`approx-gateway.service`（系統單元，專用使用者 `approx-gw`，獨立 data dir 與控制器端口）。

**理由**：宿主可能已經跑著自己的代理客戶端。把 VM 網關混進去會讓兩者的設定生命週期與故障域互相牽連。獨立實例可以單獨重啟、單獨換上游、單獨回滾。

## D4　代理不由本倉庫管理，只接受上游位址

**選擇**：`config.yaml` 只描述 `gateway.upstream`（`http://` 或 `socks5://`，可帶帳密）。網關把 VM 流量全部送過去，規則為 `MATCH,UPSTREAM`。

**理由**：

- 代理的部署、訂閱、節點選擇、健康檢查屬於另一個系統，不該被複製進這個倉庫
- 國內外分流發生在**上游**：上游收到 CONNECT 請求時看到域名，自己決定直連或走節點。本倉庫不需要、也不應該知道分流規則
- 使用者換代理只要改一行設定，不必重新渲染訂閱

**修正（2026-11-02，實測）**：上面的「前提」對 approx 的 listener 不成立。26 個 listener 全部是 `type: mixed`、`proxy: <單一節點>`，是一條直通管，不含任何規則。宿主自己的 278 條規則只作用在它的主入口 `mixed-port: 7890`，與這些 listener 無關。

實測：客體查 `myip.ipip.net` 回報「美国 乔治亚州 亚特兰大」，也就是國內站點也走美國節點。

**決定**：不做分流，全量走節點。理由是分流會讓國內流量以宿主真實公網 IP 出去，與「VM 不帶可識別身分」的目標相反。代價是國內訪問繞一圈，對 Claude Code 的實際負載影響很小。

**否決的替代**：讀取代理客戶端的訂閱檔並自行渲染規則引擎（把代理管理耦合進來，且要處理 `mixed-port` 埠衝突、listener 位址衝突、節點命名等一堆與本專案無關的細節）。

## D5　儲存：virtiofs

**選擇**：`<filesystem type='mount' driver type='virtiofs'/>` + `memfd` 共享記憶體後端。

**理由**：`~/workspace` 35G，不可能複製。9p 在 metadata 密集操作（`git status`、`rg`）上明顯偏慢，而 Claude Code 的工作負載正好是這一類。

**已驗證**：QEMU 6.2 內建 `/usr/lib/qemu/virtiofsd`，Ubuntu 22.04 的 apt 無獨立套件，libvirt 會自動偵測。

**風險**：C 版 virtiofsd 在 QEMU 7.0 後被移除；`accessmode=passthrough` 要求客體 uid 與宿主一致。

## D6　宿主服務：nftables DNAT + `route_localnet`

**選擇**：`host_fwd` 鏈把 `10.66.0.1:<port>` DNAT 到 `127.0.0.1:<port>`，端口集合宣告在 `host_svc`。

**理由**：宿主多數資料庫只綁 loopback。改它們的 `listen_addresses` 是侵入式變更，且會被套件升級覆蓋。DNAT 是純網路層，零行程開銷。

**前提**：`net.ipv4.conf.virbr-approx.route_localnet = 1`，否則核心會把 DNAT 到 127.0.0.1 的封包判定為 martian 丟棄。

## D7　元件可移植，而非另開一台網關 VM

**選擇**：網關寫成自成一體的元件（模板 + 單元 + 狀態腳本），宿主側部署只是它的一種落地方式。

**理由**：需求是單台 VM。多一台網關機器帶來的是可重建性，代價是多一跳與多一層維護。真正需要「跨宿主複用」的是**程式碼**，不是**機器**。

## D8　跨宿主複用：零硬編碼

**選擇**：所有可變項收斂到 `config.yaml`；`lib/render.py` 負責驗證與渲染；`bin/claude-vm` 只做系統操作。

**具體要求**：

- 不得出現特定使用者家目錄、固定網段、固定上游位址的硬編碼
- 模板用 `@@VAR@@`，渲染時檢查有無殘留佔位符
- `check` 驗證網段衝突、埠佔用、`/dev/kvm`、`virtiofsd`、核心二進位、上游可連性
- 客體 uid 由設定決定，不假設 1000

## D9　工具鏈與快取：全部讀寫共享

**選擇**：`/usr/local/go`、`~/.nvm`、`~/go`（GOPATH/GOMODCACHE）、`~/.cache/go-build`、`~/.npm` 全部以讀寫方式掛入客體，路徑與宿主一致。

**理由**：這些目錄合計約 6.9G。客體內重裝一份等於白佔磁碟，而且版本會漂移。共享後客體不需要安裝 Go 或 Node。

**權限如何解決**：`accessmode=passthrough` 讓客體 uid 直接對應宿主 uid。`config.yaml` 的 `guest.uid` 必須等於宿主檔案擁有者（本機為 1000），否則客體看到的是別人的檔案，寫入會失敗。

**已知風險**：

| 風險 | 說明 | 緩解 |
|------|------|------|
| 並發寫入 | 宿主與客體同時 `go build` 會寫同一份 GOCACHE | Go 的快取是內容尋址 + 原子 rename，實務上安全；但 `GOMODCACHE` 的下載鎖在 virtiofs 上未必可靠 |
| 客體可改壞宿主工具鏈 | Claude Code 會自主執行命令 | 接受。要收緊就改 `read_only: true` |
| `nvm install` 會動到宿主 | 同上 | 接受 |

**注意**：`/usr/local/go` 是 `root:root 755`，客體以 uid 1000 只能讀——即使掛載為讀寫，實際效果仍是唯讀。這是檔案權限的自然結果，不是設定。

## D10　QUIC 處理

**選擇**：上游是 HTTP 代理時，在 nft 層 `reject` UDP/443。

**理由**：HTTP CONNECT 無法承載 UDP，QUIC 必然失敗。若不主動 reject，瀏覽器會等到逾時才回退到 TCP，體感極差。`reject` 送 ICMP port unreachable，回退幾乎立即。

**若上游是 socks5 且支援 UDP**：`drop_quic: no`，QUIC 直通。

## D11　多台 VM：共用網路，per-VM 網關

**選擇**：一個網段、一張 nft 表、一組 fwmark 與路由表；每台 VM 一個 mihomo 實例、一個上游、一組埠位。分派靠 nft 的 `ip saddr`。

**理由**：TPROXY 與 REDIRECT 都是把封包改寫到指定埠，核心按埠做 socket 查找。只要每台實例監聽不同埠，`fwmark 0x1` 與 `table 100` 就能共用，不需要 per-instance 路由命名空間。這比「每台一個網段、一個網橋、一個網關」省掉 N 個 subnet、N 個 libvirt network、N 組 sysctl。

**前提**：每台 VM 需要穩定的 IP，因此用 libvirt 的 `<dhcp><host mac= ip=/>` 靜態綁定，並在 domain XML 顯式指定 MAC。

**否決的替代**：每台獨立網段（隔離更徹底，但網段與防火牆規則數量隨台數線性增長，且對「同宿主、少台數」是過度設計）。

## D12　身分邊界：哪些可以共用，哪些不行

**已驗證的機制**（靜態分析 `~/.local/share/claude/versions/2.1.316`，見 `docs/claude-identity.md`）：

- `machineID` 與 `userID` 都是**本機 `crypto.randomBytes(32)` 隨機產生後持久化**在 `~/.claude.json`，不從 `/etc/machine-id`、hostname、MAC 或 CPU 推導
- 兩者都沒有「登入後被伺服器值覆寫」的路徑
- OpenTelemetry 的 `host.id` 另外讀 `/etc/machine-id`，與 config 的 `machineID` 是兩個欄位
- 客體的 `/etc/machine-id` 由 systemd 首次開機產生（基礎映像該檔為 0 byte），且實際上等於 libvirt 的 domain UUID

**結論**：只要 `~/.claude` 不共用，每台的 `machineID` / `userID` / `host.id` 自然不同。不需要額外處理。

**禁止掛載**（`lib/render.py` 的 `DENY_MOUNTS` 強制拒絕，包含其祖先目錄）：

```
~/.claude  ~/.config/claude  ~/.ssh  ~/.config/gh
~/.aws     ~/.config/gcloud  ~/.netrc  ~/.git-credentials
```

**可以共用**：`/usr/local/go`、`~/.nvm`、`~/go`、`~/.cache/go-build`、`~/.npm`、`~/programs`、`~/.kube`、`~/.docker`。前六項是工具鏈與快取，沒有身分；後兩項是憑證，屬於已知的取捨（見下）。

**每台獨立**：`~/.ssh`（客體首次開機自行 `ssh-keygen`，宿主用 `.state/keys/<name>` 這把專用金鑰管理）、git 身分（`<name>@claude-vm.local`，不沿用宿主）、工作目錄。

**仍然共用的身分向量**：`~/workspace`（同一批 repo 與 `.git/config`）、`~/.kube`、`~/.docker`、以及所有 VM 共用一個上游與出口。這些是明示的取捨，不是遺漏。
