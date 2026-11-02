# 首次部署實錄（2026-11-02）

記錄實際 bring-up 過程中遇到的問題與處置。**代理轉發尚未打通**，見文末。

## 已修復

### 1. DHCP 被 nft input 鏈丟棄

`input` 鏈結尾的 `drop` 把客體的 DHCP 廣播（0.0.0.0:68 → 255.255.255.255:67）一起吃掉了，
VM 開機後拿不到租約。

**修**：`input` 鏈顯式放行 `udp dport { 67, 68 }`。

### 2. UFW 在 nft accept 之後仍會 DROP

UFW 的 base chain 與本表同掛 `filter`，`accept` **不會**終止其他 base chain 的評估。
DNS 查詢在我們的 nft 放行後仍被 UFW 丟掉，症狀是「計數器在漲但應用收不到」。

**修**：`ufw allow in on virbr-approx`，讓 UFW 對整個網橋放行，實際過濾仍由本表負責。
`bin/claude-vm gateway` 會自動加，`destroy` 會自動刪。

**代價**：本表若載入失敗，該網橋變成 UFW 全開（fail-open）。

### 3. mihomo 綁 IPv6 雙棧 socket，破壞 IPv4 透明代理

`bind-address` 設 `0.0.0.0` 無效，mihomo 仍綁 `[::]`。後果：
- IPv4 的 TPROXY 查找匹配不到 socket
- REDIRECT 後 `SO_ORIGINAL_DST` 取不到原始目的地（回報 `10.66.0.1:17892`）

**修**：systemd 單元 `RestrictAddressFamilies` 移除 `AF_INET6`，Go 改用 IPv4 綁定。
現況：`0.0.0.0:17853 / 17892 / 17893`。

### 4. 同一 hook 同一 priority 的兩個 nat chain 互相干擾

`host_fwd` 與 `tcp_redirect` 都在 `dstnat`。合併為單一 `prerouting_nat` 鏈。

### 5. nft -f 非冪等

對已存在的表重複定義鏈會報 `Operation not supported`。
**修**：規則集開頭加 `table ip approx_gw` + `delete table ip approx_gw`。

### 6. cloud-init write_files 指定 owner 失敗

`write_files` 執行時客體使用者可能尚未建立，`owner: claude:claude` 報
`getpwnam(): name not found`。**修**：先寫到 `/etc/claude-vm/go-env`，
再由 runcmd `install` 到定位。

### 7. sudo 下的路徑展開

`~` 在 sudo 下展開成 `/root`。**修**：`render.py` 以 `SUDO_USER` 的家目錄為基準。

### 8. 範例設定的 MAC 與註解矛盾

註解說避開 QEMU OUI，值卻是 `52:54:00`。改為 `f0:9f:c2`。

## 已驗證可用

| 項目 | 狀態 |
|------|------|
| libvirt 網路 `virbr-approx` / 10.66.0.1 | 正常 |
| DHCP 派發（含 DNS 位址 10.66.0.1） | 正常 |
| VM 開機、SSH | 正常 |
| virtiofs 七個掛載 | 全部 active，`rw` |
| Go 1.26.6（宿主共享） | `go version` 正常 |
| Go 環境檔（8 鍵，含 goproxy.cn） | 已寫入 `~/.config/go/env` |
| DNS 劫持 → mihomo:17853 | 正常，回真實 IP |
| 上游代理連通 | 正常（45.12.13.139） |

## 已解決：透明轉發（最後一個問題）

**症狀**：客體 DNS 正常，但所有 TCP 連線失敗。

**根因**：本專案的 nat 鏈原本掛在 `priority dstnat`（-100），與 **iptables 的 nat 表**
以及 **Docker 的 nat 鏈**同級。同級時它們先被評估並 `accept`，本鏈的 REDIRECT
永遠不生效。

對照組證明了這一點：`dns_hijack` 掛在 `-160`（更早），所以 DNS 一直正常。

**修**：`prerouting_nat` 改為 `priority dstnat - 5`（-105），跑在 iptables/Docker 之前。

**修復後**：
```
api.ipify.org  → 45.12.13.139        （上游出口 IP）
www.baidu.com  → HTTP 200
mihomo 日誌：10.66.0.117 --> api.ipify.org:443 match Match using UPSTREAM
```

域名被正確還原，代表 `SO_ORIGINAL_DST` 也恢復正常。

## 最終架構

| 用途 | 機制 | 優先級 |
|------|------|--------|
| DNS 劫持 | nat REDIRECT → 17853 | `-160` |
| TCP 代理 | nat REDIRECT → 17892 | `-105` |
| 宿主服務 | nat DNAT → 127.0.0.1 | `-105` |
| UDP 代理 | filter TPROXY → 17893 | `mangle` (-150) |
| 輸入過濾 | filter | `-10` |
| 轉發過濾 | filter | `-10` |

## 驗證結果

| 項目 | 結果 |
|------|------|
| 出口 IP | 45.12.13.139（上游節點） |
| 國內站點 | HTTP 200 |
| 域名還原 | `www.baidu.com:443` 正確 |
| virtiofs | 7 個掛載全部 active |
| Go | 1.26.6，`GOPATH`/`GOCACHE` 指向共享目錄，`go run` 通過 |
| Node | v25.0.0（nvm 共享） |
| Claude Code | 2.1.316 |
| cloud-init | 首次失敗（DNS 未通時 apt 掛掉），手動補完 |

## 對後續部署的影響

cloud-init 的失敗是連鎖反應：DNS 不通 → apt 失敗 → runcmd 中止 → 工具鏈設定沒寫入。
**根因已修**，重新部署時 cloud-init 應該一次通過。

---

# 第二次部署：乾淨重建（同日）

第一次部署是手動補完的。為確認 cloud-init 端到端可用，完整 `destroy` + `up` 重跑一次。

## 結果

**cloud-init：`status: done`，`errors: []`，一次通過。**

| 項目 | 結果 |
|------|------|
| Claude Code | 2.1.316 |
| Go | 1.26.6 |
| Node | v25.0.0 |
| virtiofs | 7 個掛載 |
| 出口 IP | 45.12.13.139 |
| 國內站點 | HTTP 200 |
| `go get` 經 goproxy.cn | 成功 |
| `go build` | 成功，GOCACHE 寫入共享目錄 |

## 重建時新抓到的三個問題

### 9. Go 環境偵測在 sudo 下靜默失敗

`up` 以 root 執行，`go` 不在 `secure_path`，`subprocess` 拋 `FileNotFoundError`，
偵測函式回傳空字典，客體拿到一份只有註解的環境檔。
**修**：依序嘗試 PATH、`/usr/local/go/bin/go`、`~/go/bin/go`、`~/.local/bin/go`。

### 10. 讀到 root 的 Go 環境而非使用者的

即使找到 `go`，root 會讀 `/root/.config/go/env`，拿到的 `GOPROXY` 是預設的
`proxy.golang.org`，與使用者的 `goproxy.cn` 不同。
**修**：呼叫 `go env` 時把 `HOME` 指向呼叫者的家目錄。

### 11. 渲染產物在 sudo 下變成 root 擁有

之後非特權指令讀不到 `.state/rendered/`。
**修**：渲染結束後把輸出目錄 chown 回 `SUDO_USER`。

### 12. destroy 沒有移除 UFW 規則

`ufw status` 的格式是 `Anywhere on <bridge>   ALLOW   Anywhere`，
比對字串寫成 `ALLOW.*on <bridge>` 永遠不匹配。
**修**：改為比對 `on <bridge>`。

### 13. cmd_vm 的 SSH 提示位址錯誤

印出的是閘道 `10.66.0.1`，實際是 DHCP 租約位址。
**修**：從 `virsh net-dhcp-leases` 取。

## 教訓

前一次部署之所以「通過」，是因為大部分步驟是我手動補的。**乾淨重建才會暴露
順序與權限問題**——這次抓到的三個 sudo 相關缺陷，全都是手動補完時不會出現的。

## 2026-11-02　多台 VM 改版（方案 A′）

把單台設計改成「共用網路、per-VM 網關」。以下問題只有實際跑起來才會出現。

### 12. `virsh define` 對已存在的 domain 失敗

**症狀**：`error: domain 'claude-vm' already exists with uuid ...`
**原因**：XML 沒有 `<uuid>`，libvirt 每次 define 都重新生成，與既有 UUID 衝突。
**修**：`domain.xml.tmpl` 加 `<uuid>`，由 VM 名稱派生 UUIDv5。既有 VM 用 `virsh domuuid` 取值寫進 config，保持不變。

順帶發現：客體的 `/etc/machine-id` 等於 libvirt domain UUID 去掉連字號——systemd 首次開機時取自 SMBIOS UUID。

### 13. 改了 `network.xml` 但網路定義沒有更新

**症狀**：VM 拿到 10.66.0.108 而非綁定的 .107，nft 的 `ip saddr .107` 規則全部不匹配，客體完全沒有出口（UFW 的 forward policy 把轉發流量 DROP 了）。
**原因**：`cmd_network` 只在網路不存在時才 define，既有的定義不會更新。
**修**：`cmd_network` 改用 `virsh net-update add ip-dhcp-host --live --config` 逐台補綁定。不能用 net-destroy/start，那會把既有 VM 的連線一起打斷。

### 14. cloud-init 不會因為設定變更而重跑

**症狀**：改版後客體的 git 身分仍是宿主的、客體金鑰沒有產生。
**原因**：`instance-id` 沒變，cloud-init 認定已經跑過。
**處置**：既有 VM 手動補（`ssh-keygen` + 重寫 `~/.gitconfig`）。新建的 VM 不受影響。
**未解**：沒有機制讓設定變更傳播到既有 VM。若之後需要，可以讓 `instance-id` 帶 user-data 的雜湊，代價是每次改設定都會觸發完整重跑（apt + 下載，數分鐘）。目前選擇「重建」而非「熱更新」。

### 15. 下載指令沒有逾時，cloud-init 會永久卡住

**症狀**：VM2 卡在 `curl -o /usr/local/bin/kubectl` 超過 5 分鐘，log 不再前進，`cloud-init status` 永遠 `running`。
**原因**：`curl -fsSL` 沒有 `--connect-timeout` / `--max-time`，慢速或半死的端點會讓它無限等待。
**修**：所有下載加上 `--connect-timeout 10 --max-time 180~300`。VM1 當初沒踩到是因為當時網路順。

### 16. 共用 known_hosts 造成主機金鑰衝突

**症狀**：`WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED`。
**原因**：`ssh` 預設寫 `~/.ssh/known_hosts`，而 DHCP 位址會在不同 VM 之間換手，同一個 IP 對到不同主機金鑰。
**修**：`vm_ssh` 改用每台專屬的 `.state/keys/<name>.known_hosts`，不污染使用者的檔案。

### 驗證結果

| 項目 | claude-vm | claude-vm-2 |
|---|---|---|
| 上游 | us-usa | uk-aws-direct |
| 出口 IP | 45.12.13.139 | 3.8.101.180 |
| MAC | 52:54:00:a2:a7:24（沿用） | f0:9f:c2:4f:45:07（派生） |
| 埠位 | 17892/17893/17894 | 17902/17903/17904 |

VM2 的 cloud-init 在 UK 節點上較慢，kubectl 與 miniconda 因逾時而跳過（有 `|| echo` 保護，不影響其餘步驟）。
