# 驗證清單

分階段驗證，每階段通過才進下一階段。

## Phase 0　設定與渲染（已通過）

```bash
bin/claude-vm check          # 前置條件 + 網段衝突 + 埠佔用
bin/claude-vm render
nft -c -f .state/rendered/approx-gw.nft
```

已驗證：nft 語法、XML/YAML 結構、`mihomo -t` 通過、規則為 `MATCH,UPSTREAM`。

## Phase 1　網路與網關（不建 VM）

```bash
sudo bin/claude-vm network
sudo bin/claude-vm gateway
bin/claude-vm status

# 預期
ip -br addr show virbr-approx          # 10.66.0.1/24
nft list table inet approx_gw
ip rule show | grep 'fwmark 0x1'
sysctl -n net.ipv4.conf.virbr-approx.route_localnet   # 1
ss -tlnp | grep 17893
```

**通過標準**：mihomo 進程存活、tproxy 埠監聽、nft 各鏈計數器存在。

若 mihomo 起不來，先看 `journalctl -u approx-gateway`——最常見是設定檔權限（必須是 `approx-gw` 可讀的 0600）或埠衝突。

## Phase 2　VM 與 cloud-init

```bash
sudo bin/claude-vm image
sudo bin/claude-vm vm
sudo virsh console claude-vm            # Ctrl+] 離開
```

**通過標準**：`claude --version` 有輸出。這代表 apt 與安裝器都成功穿透代理。

若卡在 `package_update`，查 `nft list table inet approx_gw` 的計數器與 `journalctl -u approx-gateway`。

## Phase 3　virtiofs

```bash
mount | grep virtiofs
ls /home/claude/workspace
touch /home/claude/workspace/.rw-test && rm /home/claude/workspace/.rw-test
go version && go build ./...           # 在某個 Go 專案下
node -v && npm -v
rg --files | wc -l
```

**通過標準**：讀寫正常、宿主端檔案擁有者不變、`go build` 不因檔案鎖失敗、唯讀掛載寫入被拒。

## Phase 4　宿主服務

```bash
nc -zv 10.66.0.1 5432
nc -zv 10.66.0.1 6379
nc -zv 10.66.0.1 8123
nc -zv 10.66.0.1 8080     # 期望失敗
psql -h 10.66.0.1 -U <vm_user> -d postgres -c 'select 1'
```

## Phase 5　隱蔽性抽查

```bash
dig +short api.anthropic.com      # 真實 IP，不是 198.18.x.x
ip -6 route                        # 應為空
curl -s https://www.baidu.com >/dev/null && echo "CN 站點可達"   # 驗證上游分流正常
nmap -p 1-10000 10.66.0.1          # 只應看到 host_services 白名單
ip neigh show                      # 閘道 MAC 不應是 52:54:00 開頭
curl -s https://api.ipify.org      # 節點 IP
traceroute -n 8.8.8.8 | head -3    # 預期缺第一跳，記錄下來
```

## Phase 6　端到端

```bash
bin/claude-vm verify
```

驗證網關單元、nft、策略路由、SSH 可達、出口 IP、DNS 非 fake-ip。
