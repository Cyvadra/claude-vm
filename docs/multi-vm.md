# 多台 VM：共用網路、每台獨立網關

狀態：**計畫，未實作**

原訂「方案 A：共用網關」已作廢。每台 VM 需要獨立出口，網關必須 per-instance；但網路層仍共用一個網段、一張 nft 表、一組策略路由。

## 目標

一個 clone 管理 N 台 VM。

- 共用：libvirt 網路、`virbr-approx`、單一 nft 表、fwmark 與路由表、mihomo 二進位、系統使用者 `approx-gw`
- 每台獨立：domain、磁碟、cloud-init seed、資源量、hostname、MAC、靜態 IP、**網關實例與上游代理**、SSH 金鑰、git 身分、工作目錄

## 非目標

- 不同網段或 L2 隔離（所有 VM 同一個 `10.66.0.0/24`，彼此可互見）
- 自動擴容、排程、編排
- 隱藏「這是虛擬機」這件事

## 上游代理的實際形態（實測，2026-11-02）

這一節修正 `docs/decisions.md` 的 D4。原文假設「上游是會分流的入口，國內外分流在上游做」。**對目前這套 approx 上游，這個假設不成立。**

`~/.local/share/approx-client/subscription.yaml` 有 26 個 listener，全部是：

```yaml
- name: us-usa
  type: mixed          # HTTP + SOCKS5 同埠
  listen: 127.0.1.16
  port: 17890
  proxy: approx-us-usa  # 直接綁單一節點
```

每個 listener 是一條**直通單一節點的管子**，不經過任何規則。宿主那 278 條規則（含 `MATCH,PROXY`）只作用在宿主自己的主入口 `mixed-port: 7890`，與這些 listener 無關。

實測結果：

| 上游 | 出口 IP |
|---|---|
| `us-usa.approx.internal:17890` | 45.12.13.139（美國喬治亞） |
| `uk-aws-direct.approx.internal:17890` | 3.8.101.180（英國） |

客體內查國內站點：

```
$ curl -s https://myip.ipip.net
当前 IP：45.12.13.139  来自于：美国 乔治亚州 亚特兰大
```

**結論：目前 VM 的所有流量（含國內站點）都從美國節點出去。原始需求「代理自動區分國內外流量」並未實現，先前的說明是錯的。**

### 已決定的處置：不做分流，全量走節點

既然上游不提供分流，而分流會讓國內流量以宿主真實公網 IP 出去，決定**維持全量走節點**，網關規則保持 `MATCH,UPSTREAM`。

代價與收益：

- 國內站點訪問繞經節點，延遲增加。對 Claude Code 的實際負載（`api.anthropic.com`、GitHub、套件源）影響很小
- 換來的是 VM 對外只有一個出口，不暴露宿主 IP，行為一致、可預測
- **連帶簡化**：不需要 `geoip.metadb`、`GeoSite.dat`，網關不依賴任何地理資料檔，也就沒有資料過期的問題

曾考慮但否決的方案：網關加 `GEOSITE,cn,DIRECT` / `GEOIP,CN,DIRECT`。否決理由是它會把宿主的真實 IP 暴露給國內站點，與「VM 不帶可識別身分」的目標相反。

`docs/decisions.md` 的 D4 需要在 Phase 5 修正：原文說「分流在上游做」，實際上上游是單節點直通，本專案選擇不做分流。

### SOCKS5 可用，QUIC 可能可以放行

`mixed` listener 同時接受 HTTP 與 SOCKS5，實測兩者都能用（同一出口 IP）。目前網關用 `type: http` 上游，HTTP CONNECT 不能承載 UDP，所以才要 `reject udp/443`。若改用 `socks5://`，且節點的 `udp=true`（26 個節點全部是），QUIC 有機會直接放行。

這一點還沒實測（SOCKS5 UDP ASSOCIATE 要走 UDP，`curl` 測不出來），列為 Phase 3 的驗證項。若成立，`drop_quic` 可以關掉，流量形態更自然。

## 為什麼可以只換網關而不換網路

TPROXY 與 REDIRECT 都是把封包改寫到**指定埠**，核心按埠做 socket 查找。只要每個 mihomo 實例監聽不同的 redir/tproxy/dns 埠，`fwmark 0x1` 與路由表 `100` 就能共用，不需要 per-instance 的路由命名空間。

因此分派點只有一個：nft 的 `ip saddr`。每台 VM 一個靜態 IP，規則按來源位址指向各自的埠。

這比「每台一個網段、一個網橋、一個網關」少掉：N 個 subnet、N 個 libvirt network、N 組 sysctl、N 份防火牆規則。

## 設定檔結構

```yaml
vm_defaults:
  vcpu: 4
  memory_gb: 8
  disk_size_gb: 60
  image_url: https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

network:                     # 全域
  name: approx-vm
  bridge: virbr-approx
  subnet: 10.66.0.0/24
  gateway: 10.66.0.1
  host_lan: 192.168.1.0/24

gateway:                     # 全域預設，可被 per-VM 覆寫
  dns_upstream: https://1.1.1.1/dns-query
  drop_quic: auto
  host_services: [3306, 5432, 6379, 8123, 9000]
  core_bin: ~/.local/share/approx-client/bin/mihomo
  port_base: 17890           # 每台 VM 的埠從這裡派生

guest:
  user: claude
  uid: 1000
  timezone: America/New_York

mounts:                      # 全域：工具鏈與快取，所有 VM 共用
  - { source: /usr/local/go,        target: /usr/local/go,        tag: go-root }
  - { source: ~/.nvm,               target: ~/.nvm,               tag: nvm }
  - { source: ~/go,                 target: ~/go,                 tag: gopath }
  - { source: ~/.cache/go-build,    target: ~/.cache/go-build,    tag: gocache }
  - { source: ~/.npm,               target: ~/.npm,               tag: npm-cache }
  - { source: ~/programs,           target: ~/programs,           tag: programs }

vms:
  - name: claude-vm
    mac: "52:54:00:a2:a7:24"        # 沿用現值，見「遷移」
    ip: 10.66.0.107                 # 沿用現值
    upstream: http://us-usa.approx.internal:17890
    mounts:
      - { source: ~/workspace, target: /home/claude/workspace, tag: workspace }

  - name: claude-vm-2
    upstream: http://uk-aws-direct.approx.internal:17890
    mounts:
      - { source: ~/workspace, target: /home/claude/workspace, tag: workspace }
```

**埠位派生**：第 i 台（從 0 起）`redir = port_base + i*10`、`tproxy = +1`、`dns = +3`、`controller = 9099 + i`。可 per-VM 覆寫，`check` 驗證無碰撞。

**MAC 派生**：未指定時 `f0:9f:c2:` + `sha256(name)` 前三 byte。不用 `52:54:00`（QEMU OUI）。MAC 必須唯一，且與網橋 MAC 不同。

**靜態 IP**：渲染成 libvirt 的 `<dhcp><host mac= ip=/></dhcp>`，DHCP 仍由 dnsmasq 發放，只是綁定。nft 靠這個位址分派。

## 不再共用的部分

| 項目 | 原狀 | 改為 |
|---|---|---|
| `~/.ssh` | 掛載宿主目錄 | 客體首次開機 `ssh-keygen -t ed25519`，`bin/claude-vm sshkey <name>` 取回公鑰由使用者自行貼到 GitHub |
| `~/.config/gh` | 掛載 | 不掛載。要用就進 VM 自己 `gh auth login` |
| git 身分 | 從宿主 `git config` 讀取寫入 | 每台生成 `<name>` / `<name>@claude-vm.local`，不帶任何宿主資訊 |
| `~/.kube`、`~/.docker` | 掛載 | **建議一併移除**（憑證類，且與 git 無關）。待確認 |

`detect_git_identity()` 從 `render.py` 移除。

## 身分機制的實測結論（docs/claude-identity.md）

對 `claude` 二進位做靜態分析的結果，直接影響本設計的驗收標準。

**關鍵事實**：`machineID` 與 `userID` 都是**首次執行時本機 `randomBytes(32)` 產生的 64 字元 hex，寫進 `~/.claude.json` 後固定**。與 hostname、MAC、CPU、`/etc/machine-id` 全都無關，也沒有「登入後由伺服器下發覆寫」的路徑。

這意味著：**只要 `~/.claude.json` 不共用，每台 VM 天生就是一個不同的裝置。** 不需要偽造任何東西。

| 欄位 | 存放 | 換機是否變 |
|---|---|---|
| `machineID` | `~/.claude.json` | 變（新檔新隨機值） |
| `userID` | `~/.claude.json` | 變 |
| `remoteControlMachineId`、`summonSidKey` | `~/.claude.json` | 變 |
| `host.id`（OpenTelemetry） | 執行時讀 `/etc/machine-id` | 變（已驗證基礎映像該檔為 0 byte，systemd 首次開機產生） |
| `accountUuid`、`email` | 憑證檔，伺服器下發 | 不變——**這是帳號層，換機器改不了** |

上報內容：`https://api.anthropic.com/api/event_logging/v2/batch`，payload 含 `device_id`（= `userID`）、`session_id`、`email`、`agent_id`。另有 Datadog RUM 與 Sentry。請求 header 帶 `X-Claude-Code-Session-Id`、`x-app: cli`、`claude-code/<version>`。

### 對設計的三個具體含意

1. **`~/.claude.json` 與 `~/.claude/` 絕不可共用**。一旦共用，多台 VM 會回報同一個 `device_id`，不同帳號掛在同一個 device 上——這正是要避免的關聯。現有的拒絕清單已涵蓋，見下
2. **拒絕清單要檢查「祖先路徑」**，不能只做字串比對。掛載 `~` 或 `~/.config` 會連帶暴露 `.claude.json` 與 `.ssh`，字串比對抓不到
3. **`accountUuid` 與 `email` 是帳號層，本專案無法處理**。若封禁原因是付款方式或帳號本身，機器層的隔離不會有任何幫助

## 強制約束

`validate()` 新增拒絕清單：`mounts` 的 source 解析後若落在下列路徑之下，直接拒絕渲染。

```
~/.claude  ~/.config/claude  ~/.ssh  ~/.config/gh
~/.aws     ~/.config/gcloud  ~/.netrc  ~/.git-credentials
~/.kube    ~/.docker
```

**比對方式是路徑包含，不是字串相等**：source 解析後若**等於或為**任一受保護路徑的祖先，一律拒絕。掛載 `~` 或 `~/.config` 會連帶暴露 `.claude.json` 與 `.ssh`，單純的字串比對抓不到這種情形。

理由：這些目錄承載帳號層或宿主層的身分，掛進 VM 等於把多台機器綁成同一身分。把「不共用身分」從文件約定變成程式約束，避免日後有人手滑加回來。

具體到 Claude Code：`~/.claude.json` 存著 `machineID`、`userID`、`remoteControlMachineId`、`summonSidKey`，全部是裝置層識別。共用它等於讓多台 VM 回報同一個 `device_id`。

## 命令介面

```
init                    建立 config.yaml
check [name]            前置檢查（全域一次，VM 逐台）
render [--only shared|name]
ls                      列出 VM：名稱、狀態、IP、上游、出口 IP
image [name|--all]
network                 （全域，不變）
gateway [name|--all]    安裝並啟動 per-VM 網關實例
vm [name] [--redefine]
up [name|--all]
status [name]
start|stop|console <name>
sshkey <name>           印出該台客體產生的 SSH 公鑰
destroy <name> [--purge]  只拆該台 domain 與網關實例
teardown                拆除全域層（網路、nft、策略路由）
verify [name|--all]     含跨 VM 唯一性與出口 IP 差異檢查
```

**向下相容**：只有一台 VM 時 `<name>` 可省略。

**行為變更**：`destroy` 從「拆光」改為「只拆這台」。拆全域層改走 `teardown`。

## 渲染產物

```
.state/rendered/
  shared/
    network.xml                含所有 VM 的靜態 DHCP 綁定
    approx-gw.nft              單表，per-VM saddr 分派
    approx-gw.sysctl
    systemd/approx-gateway-net.service
    systemd/approx-gateway-net
    systemd/approx-gateway@.service
  vms/<name>/
    domain.xml
    mihomo/config.yaml         該台的網關設定（上游、埠、data dir）
    cloud-init/user-data.yaml
    cloud-init/meta-data
```

`render.py` 的 `shutil.rmtree(out)` 改為只清對應子樹。

## nft 結構變化

現行是單組規則，改為按來源位址分派。三處要改：

```
chain dns_hijack {           # priority -160
    iifname "virbr-approx" ip saddr 10.66.0.107 udp dport 53 redirect to :17893
    iifname "virbr-approx" ip saddr 10.66.0.108 udp dport 53 redirect to :17903
}

chain prerouting_nat {       # priority dstnat - 5
    iifname "virbr-approx" ip saddr 10.66.0.107 meta l4proto tcp redirect to :17890
    iifname "virbr-approx" ip saddr 10.66.0.108 meta l4proto tcp redirect to :17900
}

chain prerouting_tproxy {    # priority mangle
    iifname "virbr-approx" ip saddr 10.66.0.107 meta l4proto udp tproxy ip to :17891 meta mark set 0x1 accept
    iifname "virbr-approx" ip saddr 10.66.0.108 meta l4proto udp tproxy ip to :17901 meta mark set 0x1 accept
}

chain input {
    meta l4proto { tcp, udp } th dport { 17890,17891,17893, 17900,17901,17903 } accept
}
```

順序不變：DNS 必須早於 TPROXY，否則 DOMAIN 規則全失效。`bypass4`、NTP、QUIC reject、forward 封鎖宿主區網等規則維持全域。

**風險**：改一張表意味著所有 VM 的連線在 `nft -f` 瞬間重載。`nft -f` 本身是原子的，既有 conntrack 不受影響，但短暫的重建期間新連線可能失敗。可接受。

## systemd 單元

```
approx-gateway-net.service      全域，套用 nft + sysctl + fwmark（不變）
approx-gateway@.service         樣板單元，實例名 = VM 名
```

`approx-gateway@<name>.service`：
- `ExecStart=/usr/local/bin/approx-mihomo -d /var/lib/approx-gateway/<name> -f /etc/approx-gateway/<name>/config.yaml`
- 沿用既有的 `AmbientCapabilities`、`RestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK`（拿掉 AF_INET6 是 TPROXY 能運作的前提）

## 遷移既有 VM

現役 `claude-vm` 會受到三處變更影響：

1. **移除 `~/.ssh`、`~/.config/gh` 掛載** → domain XML 的 filesystem 數量變少，必須 redefine 並重啟
2. **git 身分改為 per-VM** → 客體內 `.gitconfig` 需重寫
3. **靜態 IP** → config 填現值 `10.66.0.107`，避免 DHCP 重新協商

所以遷移就是一次重啟。順序：`render` → `gateway claude-vm` → `vm claude-vm --redefine` → `start`。約 30 秒停機。

客體重啟後需要手動做一次：`ssh-keygen` 產生的公鑰貼到 GitHub（`bin/claude-vm sshkey claude-vm` 會印出來）。

## 分階段與驗收

### Phase 1　設定與渲染

改 `config.example.yaml`、`lib/render.py`、`templates/domain.xml.tmpl`、`templates/cloud-init/*`、`templates/network.xml.tmpl`（靜態 DHCP）。不動宿主狀態。

**驗收**：`render --only shared` 與 `render --only claude-vm` 產出可解析；與改動前 diff，差異應僅限：domain.xml 多 `<mac>`、filesystems 少兩個、network.xml 多 `<host>` 綁定、cloud-init 少 gitconfig 而多 per-VM 身分與 ssh-keygen。

### Phase 2　命令介面與生命週期

改 `bin/claude-vm`：`resolve_vm`、`vm_ip`（按 MAC 比對租約，取代 `head -1`）、`ls`、`sshkey`、`destroy`/`teardown` 拆分、`flock` 併發鎖。

**驗收**：單 VM 設定下所有指令可省略 name，與改動前等效。

### Phase 3　per-VM 網關與 nft 分派

`approx-gateway@.service` 樣板、nft 按 saddr 分派、per-VM 設定與 data dir。

**驗收**：既有 VM 重啟後 `verify` 全綠，出口 IP 與改動前一致（同一上游）。

### Phase 4　第二台 VM，不同上游

**驗收**：
- 兩台同時 running，**出口 IP 不同**
- 各自 `verify` 通過
- `~/.claude.json` 的 `machineID`、`userID`、`remoteControlMachineId`、`summonSidKey` 兩台全部不同（皆為本機隨機產生，共用即為 bug）
- `nft` 規則中兩台的 saddr 各自指向正確的埠；`ss -tlnp` 能看到兩組獨立的 mihomo 監聽
- machine-id、hostname、MAC、SSH host key 兩台不同
- **並發壓力測試**：兩台同時 `go build` 同一模組，確認 `GOCACHE`/`GOMODCACHE` 無損壞（目前唯一未驗證的假設）
- `destroy claude-vm-2` 後，`claude-vm` 的網路與出口不受影響

### Phase 5　文件

README、decisions（新增 D11 多 VM、D12 身分邊界）、storage、verify、parity、bringup-log。

## 風險

| 風險 | 處置 |
|---|---|
| 共用 `GOCACHE`/`GOMODCACHE` 並發損壞 | Phase 4 壓測；失敗則退回 per-VM 快取（+3.3G/台） |
| `nft -f` 重載瞬間斷流 | 原子操作，可接受；文件標註 |
| 上游端點不足，湊不出 N 個不同出口 | 需先確認可用的上游清單 |
| 靜態 IP 與既有租約衝突 | `check` 驗證 IP 落在 DHCP 範圍外或已綁定 |
| 移除 `~/.ssh` 後使用者需手動貼公鑰 | `sshkey` 子命令降低摩擦 |
| 新 VM 的 cloud-init 重複下載 gh/docker/miniconda/claude（3–8 分鐘） | 已知代價；後續可考慮烘烤映像 |
| 同網段無隔離，VM 之間可互訪 | 已列為非目標 |

## 待確認

1. 第二台資源量（預設沿用 4C8G）
2. 上游是否改走 `socks5://` 以放行 QUIC，待 Phase 3 實測 SOCKS5 UDP 後決定
3. `~/.kube`、`~/.docker` 已定為維持共用

## 附註

Claude Code 的身分識別機制正在由獨立的分析任務靜態分析（`docs/claude-identity.md`）。該結果出爐後，本文件的「強制約束」與 Phase 4 的驗收項可能需要調整。
