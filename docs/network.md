# 網路設計

## 拓撲

```
   claude-vm 10.66.0.107        claude-vm-2 10.66.0.108
    │ 預設閘道 10.66.0.1        │
    └────────────┬───────────────┘
                 ▼
      virbr-approx 10.66.0.1/24
       │
       ├─ nft dns_hijack          priority -160     ip saddr → 17894 / 17904
       ├─ nft prerouting_nat      priority dstnat-5 tcp ip saddr → 17892 / 17902
       ├─ nft prerouting_tproxy   priority mangle   udp ip saddr → 17893 / 17903
       └─ nft input / forward
       │
   ┌───┴─────────────────────────┐
   ▼                             ▼
approx-gateway@claude-vm    approx-gateway@claude-vm-2
 mihomo 17892/93/94          mihomo 17902/03/04
 MATCH,UPSTREAM              MATCH,UPSTREAM
   → us-usa                    → uk-aws-direct
```

所有 VM 共用一個網段、一張 nft 表、一組 fwmark 與路由表。分派點只有 `ip saddr`：每台一個靜態 IP，規則指向各自的埠。

## 封包路徑

任何站點 `https://api.anthropic.com`（以 `claude-vm` 為例）：

1. VM 向 `10.66.0.1:53` 查詢 → `dns_hijack` 按 `ip saddr 10.66.0.107` 改寫為 `17894`
2. mihomo 的 DNS 帶 `respect-rules: true`，查詢本身也走規則 → 經上游代理做 DoH
3. 回傳真實 IP（`redir-host`，不是 fake-ip）
4. VM 連向該 IP:443 → `prerouting_tproxy` 按 saddr 打上 mark `0x1` 並轉給 `17893`
5. 策略路由 `fwmark 0x1 → table 100`，`table 100` 的 `local default dev lo` 把封包投遞給透明 socket
6. mihomo 依 `MATCH,UPSTREAM` 把連線交給該台的上游代理

國內站點 `cn.archive.ubuntu.com`：

路徑完全相同。approx 的上游 listener 是**單節點直通**、不含規則，所以國內站點也從節點出去——**本專案不做分流**（D4）。

宿主服務 `10.66.0.1:5432`：

1. `tproxy` 鏈因 `ip daddr 10.66.0.1 return` 跳過
2. `host_fwd` DNAT 到 `127.0.0.1:5432`
3. `input` 鏈放行 `ip daddr 127.0.0.1 tcp dport @host_svc`

## 三個必須照抄的細節

### ① `dns_hijack` 的 priority 必須早於 `tproxy`

nftables 內建 priority：`mangle = -150`、`dstnat = -100`。**數值小的先跑**，所以 `mangle` 反而早於 `dstnat`。

若 DNS 規則與 TPROXY 同級，DNS 查詢會被當成「一條到 8.8.8.8:53 的 UDP 連線」丟進 tproxy。mihomo 的解析器永遠不被呼叫，`redir-host` 的 IP→域名映射不存在，所有 `DOMAIN-*` / `DOMAIN-SUFFIX` / `DOMAIN-KEYWORD` 規則全部失效，只剩 `GEOIP` 能匹配。

症狀：國外網站大部分還能通（GEOIP 兜底），但針對性規則全部失靈，且日誌看不出異常。極難排查。

因此 `dns_hijack` 寫死 `priority -160`。

### ② `input` 鏈的順序

TPROXY 在 `prerouting` 被 `accept` 之後，封包**仍要經過 `input` 鏈**。若直接 `drop` 掉 `virbr-approx` 的輸入，透明代理會全線不通，而 `tproxy` 鏈的計數器卻在增長——很容易誤判成路由問題。

必須先放行 `meta mark 0x1`。

### ③ `rp_filter`

反向路徑過濾會把「來源屬於 10.66.0.0/24、卻從 `lo` 進來」的封包判定為位址欺騙並丟棄。必須對 `virbr-approx` 關閉：

```
net.ipv4.conf.virbr-approx.rp_filter = 0
```

症狀：規則、路由、監聽全部正確，但完全不通。

## 隱蔽性：做到了什麼，沒做到什麼

| 措施 | 目的 |
|------|------|
| `redir-host` 而非 `fake-ip` | 避免客體 `dig` 得到 `198.18.x.x` 這個 Clash 家族指紋 |
| 主動 reject QUIC | 避免 UDP 逾時造成的異常停頓；回退行為與正常網路一致 |
| `input` 鏈預設 drop | 宿主上 80/443/888/3002/5173/25672 等服務對 VM 不可見，只暴露 `host_svc` 白名單 |
| 只宣告 IPv4 | 客體無 IPv6 預設路由，無從繞道 |
| 改寫閘道 MAC OUI | 不讓 `52:54:00` 直接暴露 QEMU |
| 網路 domain 用 `lan` | DHCP 與反解中不出現專案名 |
| NTP 直連 | 客體時鐘正常，少一個破綻 |

**已知破綻**（可接受）：

- 出口 IP 是外國的——這是目的本身
- `traceroute` 到被代理的目的地時缺第一跳（流量經節點轉發）
- ICMP 不走代理，`ping` 國外被封位址的行為與 HTTP 不一致
- 客體能掃到 `host_svc` 白名單裡的資料庫端口，看得出對面是台開發機而非路由器

**目標不是「不可偵測」，是「不像一個代理環境」。**

## 與上游代理的關係

本倉庫不產生分流規則。`MATCH,UPSTREAM` 表示該台所有流量都交給它的 `upstream`（預設取 `gateway.upstream`，可 per-VM 覆寫）。

approx 的 listener 實測是 `type: mixed`、`proxy: <單一節點>` 的直通管，不含規則，因此國內站點也會從節點出去。這是刻意的：分流會讓國內流量以宿主真實公網 IP 出去，與「VM 不帶可識別身分」的目標相反。代價是國內訪問繞一圈，對 Claude Code 的負載影響很小。
