# 宿主服務轉發

## 現況掃描（2026-11-01）

| 端口 | 服務 | 監聽位址 | 需 DNAT |
|------|------|---------|---------|
| 3306 | MySQL（寶塔 `/www/server/mysql`） | `*:3306` | 否，已綁全部介面 |
| 5432 | PostgreSQL | `127.0.0.1` | 是 |
| 6379 | Redis | `127.0.0.1` | 是 |
| 8123 | ClickHouse HTTP | `127.0.0.1` | 是 |
| 9000 | ClickHouse native | `127.0.0.1` | 是 |
| 9004 / 9005 / 9009 | ClickHouse 內部（interserver / mysql 協議） | `127.0.0.1` | 不需要 |
| 15672 / 25672 | RabbitMQ 管理與叢集 | `0.0.0.0` | 不需要，且**不應**開放 |

`host_svc` 集合預設：`3306, 5432, 6379, 8123, 9000`。MySQL 雖然已綁 `*`，仍納入集合以保持一致性——DNAT 到 `127.0.0.1:3306` 一樣可達。

## 安全提醒

VM 內執行的是 Claude Code，**它會執行任意命令**。開放資料庫端口等於把宿主資料庫的網路可達性交給一個會自主執行動作的 agent。

建議：

1. **為 VM 建專用資料庫帳號**，不要沿用宿主的管理員憑證。按庫授權，能給唯讀就給唯讀。
2. 需要寫入時，用獨立的 schema 或資料庫，不要直接指向生產庫。
3. `pg_hba.conf` / MySQL 的 `user@host` 授權只放行 `10.66.0.0/24`。
4. Redis 若無密碼，**不要**放進 `host_svc`。目前宿主 redis 綁 loopback 且無從得知是否設了密碼，開放前必須確認。

## 宿主側必要設定（實測後補）

DNAT 之後，服務看到的來源是 VM 位址（`10.66.0.x`），不是 loopback。
只綁 loopback 的服務本身沒問題，但**帶來源位址檢查的服務會拒絕**。已實測並處理：

### PostgreSQL

`pg_hba.conf` 預設只有 `127.0.0.1/32`，VM 連線會被判 `no pg_hba.conf entry`。

```bash
# 追加到 /etc/postgresql/<ver>/main/pg_hba.conf
host    all    all    10.66.0.0/24    scram-sha-256
sudo systemctl reload postgresql
```

**不採用**「把來源改寫成 127.0.0.1」的做法：本機交付的封包不經過 `postrouting`，
而 nftables 又不允許在 `prerouting` 做 SNAT（`Operation not supported`）。
這條路走不通。

### Redis

無密碼時 `protected-mode yes` 只接受 loopback 來源，VM 連線會被 `DENIED`。

```bash
sudo cp /www/server/redis/redis.conf /www/server/redis/redis.conf.bak-claudevm
sudo sed -i 's/^protected-mode yes/protected-mode no/' /www/server/redis/redis.conf
sudo systemctl restart redis.service
```

Redis 仍只綁 `127.0.0.1`，唯一的入口是本專案的 DNAT，暴露面僅限 VM 網段。
**注意**：此檔由寶塔管理，面板操作可能覆蓋，改動後需確認。

### ClickHouse / MySQL

實測無需額外設定即可連線。ClickHouse 的 `users.xml` 是 `<networks><ip>::/0</ip></networks>`，不限來源。

### 客體側：讓 127.0.0.1 直通

共用設定檔寫的是 `127.0.0.1:<port>`，在 VM 內必須也能成立。
客體由 cloud-init 安裝 `localfwd.service`，把 `127.0.0.1` 上的這些端口 DNAT 到閘道。

**關鍵細節**：postrouting 的 masquerade 只能用 `ip daddr <gateway> tcp dport {...}` 判斷，
**不能用 `ip saddr 127.0.0.1`**——後者會連 systemd-resolved 從 loopback 發出的 DNS
查詢一起改寫，導致所有 DNS 解析失敗（症狀：`resolvectl query` 正常但 `getent hosts` 掛住）。

## 增減端口

只改一處：`gateway/nft/approx-gw.nft` 的 `host_svc` 集合。

```
set host_svc {
    type inet_service
    elements = { 3306, 5432, 6379, 8123, 9000 }
}
```

然後 `nft -f` 重載規則集。不需要重啟 mihomo，也不需要動 VM。
