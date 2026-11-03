#!/usr/bin/env python3
"""從 config.yaml + templates/ 渲染部署產物（多台 VM）。

本模組不做任何系統變更：只讀設定，寫出檔案到 --out。
virsh / nft / systemctl 由 bin/claude-vm 負責。

代理本身不由本倉庫管理：設定只描述「上游代理位址」。
每台 VM 有自己的網關實例與上游，但共用一個網段、一張 nft 表、
一組 fwmark 與路由表——分派靠 nft 的 `ip saddr`。
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import uuid as uuidlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
KEYS = ROOT / ".state" / "keys"
TAG_SAFE = re.compile(r"[^a-z0-9]+")

PROXY_SCHEMES = {"http": "http", "https": "http", "socks5": "socks5", "socks5h": "socks5"}

# 需要在客體重現的 Go 環境鍵。
# 排除 GOROOT/GOPATH/GOCACHE/GOMODCACHE/GOENV 等由 $HOME 推導的路徑——
# 客體的家目錄不同，這些交給 profile.d 與掛載點處理。
GO_ENV_KEYS = [
    "GO111MODULE", "GOAMD64", "GOAUTH", "GOFLAGS", "GOINSECURE",
    "GONOPROXY", "GONOSUMDB", "GOPRIVATE", "GOPROXY", "GOSUMDB",
    "GOVCS", "GOTOOLCHAIN", "CGO_ENABLED", "GOTELEMETRY", "GODEBUG", "GOEXPERIMENT",
]

# 絕不可掛進 VM 的路徑。這些目錄承載帳號層或宿主層的身分，
# 掛進 VM 等於把多台機器綁成同一身分。
# 同時禁止掛載它們的祖先目錄（例如整個家目錄），否則約束形同虛設。
DENY_MOUNTS = [
    "~/.claude",
    "~/.claude.json",
    "~/.config/claude",
    "~/.config/claude-code",
    "~/.local/share/claude",
    "~/.codex",
    "~/.ssh",
    "~/.config/gh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.netrc",
    "~/.git-credentials",
]


def invoking_home() -> Path:
    """sudo 下 os.path.expanduser("~") 會變成 /root，這裡還原成呼叫者家目錄。"""
    import pwd
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path(os.path.expanduser("~"))


def expand(p) -> Path:
    """展開 ~ 與 $HOME，一律以呼叫者家目錄為基準。"""
    text = str(p)
    home = str(invoking_home())
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    text = text.replace("$HOME", home).replace("${HOME}", home)
    return Path(os.path.expandvars(text))


class ConfigError(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def validate_timezone(tz: str, where: str) -> None:
    """時區必須存在於宿主 tzdata，且不可含路徑穿越。"""
    require(re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz) is not None and ".." not in tz,
            f"{where} 格式無效：{tz!r}")
    require((Path("/usr/share/zoneinfo") / tz).is_file(),
            f"{where} 不存在於 tzdata：{tz}")


# ── 設定載入與正規化 ────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    require(path.is_file(), f"設定檔不存在：{path}（先執行 claude-vm init）")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(cfg, dict), "設定檔必須是 YAML mapping")
    for section in ("vm_defaults", "network", "gateway", "guest", "mounts", "vms"):
        require(section in cfg, f"設定檔缺少 {section} 區段")
    return cfg


def derive_mac(name: str) -> str:
    """由名稱派生穩定的 MAC。刻意不用 52:54:00（QEMU 的 OUI）。"""
    digest = hashlib.sha256(name.encode()).digest()
    return "f0:9f:c2:%02x:%02x:%02x" % (digest[0], digest[1], digest[2])


def derive_ip(cfg: dict, index: int) -> str:
    start = ipaddress.ip_address(cfg["network"]["dhcp_start"])
    return str(start + index)


def derive_uuid(name: str) -> str:
    """由名稱派生穩定的 domain UUID。libvirt 若自行生成會每次 define 都變，
    而客體的 /etc/machine-id 在首次開機時取自 SMBIOS UUID，穩定性有實際意義。"""
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"claude-vm:{name}"))


def ports_for(cfg: dict, index: int) -> dict:
    """每台 VM 的網關埠位。同一個 fwmark 與路由表就能分派到不同實例，
    因為核心是按埠做 socket 查找。"""
    gw = cfg["gateway"]
    base = int(gw.get("port_base", 17890))
    ctrl = int(gw.get("controller_base", 9099))
    return {
        "redir": base + index * 10 + 2,
        "tproxy": base + index * 10 + 3,
        "dns": base + index * 10 + 4,
        "controller": f"127.0.0.1:{ctrl + index}",
    }


def normalise_mount(raw, guest_user: str, where: str) -> dict:
    """接受字串簡寫或 mapping。字串時 target 依慣例推導：
    `~/x` → `/home/<user>/x`，絕對路徑 → 原樣。"""
    if isinstance(raw, str):
        raw = {"source": raw}
    require(isinstance(raw, dict) and "source" in raw, f"{where} 必須是字串或含 source 的 mapping")
    m = dict(raw)
    src = str(m["source"])
    if "target" not in m:
        if src == "~":
            m["target"] = f"/home/{guest_user}"
        elif src.startswith("~/"):
            m["target"] = f"/home/{guest_user}/{src[2:]}"
        else:
            require(src.startswith("/"), f"{where}.source 必須是 ~ 開頭或絕對路徑：{src}")
            m["target"] = src
    m.setdefault("read_only", False)
    return m


def normalise(cfg: dict) -> dict:
    """套用 vm_defaults，補齊每台的 mac / ip / 埠位，展開成完整結構。"""
    defaults = cfg["vm_defaults"]
    guest_user = str(cfg["guest"]["user"])
    # 是否掃宿主 ~/.ssh/*.pub 一併注入；預設掃。
    cfg["guest"].setdefault("ssh_keys", "scan")
    require(str(cfg["guest"]["ssh_keys"]) in ("scan", "none"),
            "guest.ssh_keys 只接受 scan（掃 ~/.ssh/*.pub）或 none")
    shared = [normalise_mount(m, guest_user, f"mounts[{i}]")
              for i, m in enumerate(cfg["mounts"])]
    cfg["mounts"] = shared
    vms, names = [], set()
    for i, raw in enumerate(cfg["vms"]):
        require(isinstance(raw, dict), f"vms[{i}] 必須是 mapping")
        require("name" in raw, f"vms[{i}] 缺少 name")
        name = str(raw["name"])
        require(re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name) is not None,
                f"vms[{i}].name 必須是 DNS label（小寫字母開頭，僅 a-z0-9-）：{name}")
        require(name not in names, f"VM 名稱重複：{name}")
        names.add(name)
        vms.append({
            "name": name,
            "index": i,
            "hostname": str(raw.get("hostname") or name),
            "timezone": str(raw.get("timezone") or cfg["guest"].get("timezone", "UTC")),
            "vcpu": int(raw.get("vcpu", defaults["vcpu"])),
            "memory_gb": int(raw.get("memory_gb", defaults["memory_gb"])),
            "disk_size_gb": int(raw.get("disk_size_gb", defaults["disk_size_gb"])),
            "image_url": str(raw.get("image_url", defaults["image_url"])),
            # `random` is resolved once when the domain is first defined.
            # Keep a deterministic placeholder here so templates remain valid
            # XML; it is never used on the wire in this mode.
            "mac_mode": "random" if str(raw.get("mac", "")).lower() == "random" else "stable",
            "mac": (derive_mac(name) if str(raw.get("mac", "")).lower() == "random"
                    else str(raw.get("mac") or derive_mac(name)).lower()),
            "uuid": str(raw.get("uuid") or derive_uuid(name)),
            "ip": str(raw.get("ip") or derive_ip(cfg, i)),
            "upstream": str(raw.get("upstream") or cfg["gateway"].get("upstream") or ""),
            "mounts": shared + [normalise_mount(m, guest_user, f"{name}.mounts[{i}]")
                                for i, m in enumerate(raw.get("mounts") or [])],
            "ports": ports_for(cfg, i),
        })
    cfg["vms"] = vms
    return cfg


# ── 驗證 ────────────────────────────────────────────────────────────

def parse_upstream(url: str) -> dict:
    """把上游代理位址解析成 mihomo 的 proxy 條目。"""
    u = urlparse(url)
    require(u.scheme in PROXY_SCHEMES,
            f"upstream 只支援 {sorted(PROXY_SCHEMES)}，收到 {u.scheme!r}（{url}）")
    require(bool(u.hostname), f"upstream 缺少主機名：{url}")
    require(u.port is not None, f"upstream 缺少埠號：{url}")
    entry = {
        "name": "UPSTREAM",
        "type": PROXY_SCHEMES[u.scheme],
        "server": u.hostname,
        "port": int(u.port),
    }
    if u.username:
        entry["username"] = unquote(u.username)
        entry["password"] = unquote(u.password or "")
    # HTTP 代理不支援 UDP；socks5 若上游支援才開啟
    entry["udp"] = PROXY_SCHEMES[u.scheme] == "socks5"
    return entry


def check_mount_allowed(source: Path, where: str) -> None:
    home = invoking_home()
    s = source.resolve()
    for entry in DENY_MOUNTS:
        denied = (home / entry[2:]).resolve() if entry.startswith("~/") else Path(entry).resolve()
        if s == denied or denied in s.parents:
            raise ConfigError(f"{where}.source {source} 位於禁止共用的路徑之下（{entry}）")
        if s in denied.parents:
            raise ConfigError(f"{where}.source {source} 包含禁止共用的路徑（{entry}）")


def mount_tag(m: dict, used: set[str], where: str) -> str:
    """virtiofs 的 target dir 是標籤不是路徑，必須唯一且穩定。"""
    if m.get("tag"):
        tag = TAG_SAFE.sub("-", str(m["tag"]).lower()).strip("-")
        require(bool(tag), f"{where}.tag 無效：{m['tag']}")
        require(tag not in used, f"mounts tag 重複：{tag}")
        used.add(tag)
        return tag
    base = TAG_SAFE.sub("-", Path(str(m["source"])).name.lower()).strip("-") or "mount"
    tag, n = base, 1
    while tag in used:
        n += 1
        tag = f"{base}-{n}"
    used.add(tag)
    return tag


def validate(cfg: dict) -> None:
    net, gw, guest = cfg["network"], cfg["gateway"], cfg["guest"]

    subnet = ipaddress.ip_network(net["subnet"], strict=True)
    require(subnet.version == 4, "network.subnet 目前只支援 IPv4")
    gateway = ipaddress.ip_address(net["gateway"])
    require(gateway in subnet, f"network.gateway {gateway} 不在 {subnet} 內")
    require(gateway not in (subnet.network_address, subnet.broadcast_address),
            "network.gateway 不可為網段或廣播位址")

    dhcp_start = ipaddress.ip_address(net["dhcp_start"])
    dhcp_end = ipaddress.ip_address(net["dhcp_end"])
    require(dhcp_start in subnet and dhcp_end in subnet, "DHCP 範圍必須在 subnet 內")
    require(dhcp_start < dhcp_end, "dhcp_start 必須小於 dhcp_end")
    require(not (dhcp_start <= gateway <= dhcp_end), "gateway 不可落在 DHCP 範圍內")

    host_lan = ipaddress.ip_network(net["host_lan"], strict=False)
    require(not subnet.overlaps(host_lan),
            f"network.subnet {subnet} 與 network.host_lan {host_lan} 重疊")

    bridge_mac = str(net["bridge_mac"]).lower()
    require(re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", bridge_mac) is not None,
            f"network.bridge_mac 格式無效：{bridge_mac}")

    dns_upstream = str(gw.get("dns_upstream", ""))
    require(dns_upstream.startswith(("https://", "tls://", "tcp://", "udp://")),
            "gateway.dns_upstream 必須是 https:// tls:// tcp:// 或 udp:// 位址")

    services = [int(p) for p in gw["host_services"]]
    require(all(1 <= p <= 65535 for p in services), "gateway.host_services 含無效端口")
    require(len(set(services)) == len(services), "gateway.host_services 有重複端口")

    require(int(guest["uid"]) > 0, "guest.uid 不可為 0（root）")
    # 全域值是預設；每台可覆寫（normalise 已把實際值放進 vm["timezone"]）
    validate_timezone(str(guest.get("timezone", "UTC")), "guest.timezone")

    macs, ips, ports = {}, {}, {}
    for vm in cfg["vms"]:
        name = vm["name"]
        require(vm["vcpu"] >= 1, f"{name}: vcpu 必須 >= 1")
        require(vm["memory_gb"] >= 2, f"{name}: memory_gb 必須 >= 2")
        require(vm["disk_size_gb"] >= 10, f"{name}: disk_size_gb 必須 >= 10")

        require(re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", vm["mac"]) is not None,
                f"{name}: mac 格式無效：{vm['mac']}")
        require(vm["mac"] != bridge_mac, f"{name}: mac 不可與 network.bridge_mac 相同")
        require(vm["mac"] not in macs, f"{name}: mac 與 {macs.get(vm['mac'])} 重複")
        macs[vm["mac"]] = name

        ip = ipaddress.ip_address(vm["ip"])
        require(ip in subnet, f"{name}: ip {ip} 不在 {subnet} 內")
        require(ip != gateway, f"{name}: ip 不可為閘道位址")
        require(ip not in (subnet.network_address, subnet.broadcast_address),
                f"{name}: ip 不可為網段或廣播位址")
        require(str(ip) not in ips, f"{name}: ip 與 {ips.get(str(ip))} 重複")
        ips[str(ip)] = name

        parse_upstream(vm["upstream"])
        validate_timezone(vm["timezone"], f"{name}.timezone")

        vm_ports = [vm["ports"]["redir"], vm["ports"]["tproxy"], vm["ports"]["dns"]]
        require(len(set(vm_ports)) == 3, f"{name}: redir / tproxy / dns 埠不可重複")
        for p in vm_ports:
            require(1 <= p <= 65535, f"{name}: 埠位超出範圍：{p}")
            require(p not in ports, f"{name}: 埠 {p} 與 {ports.get(p)} 衝突")
            ports[p] = name
        require(not set(vm_ports) & set(services), f"{name}: 埠位與 host_services 衝突")

        used: set[str] = set()
        for i, m in enumerate(vm["mounts"]):
            where = f"{name}.mounts[{i}]"
            src = expand(m["source"])
            require(src.is_dir(), f"{where}.source 不是目錄：{src}")
            require(str(m["target"]).startswith("/"), f"{where}.target 必須是絕對路徑")
            check_mount_allowed(src, where)
            mount_tag(m, used, where)


# ── 宿主環境偵測（所有 VM 共用）─────────────────────────────────────

def detect_go_env(home_src: Path, home_dst: Path) -> dict[str, str]:
    """讀取宿主 `go env`，把需要在客體重現的設定挑出來。

    sudo 的 secure_path 不含 /usr/local/go/bin，直接呼叫 "go" 會找不到，
    且 root 會讀 /root/.config/go/env，所以必須顯式指定 HOME。
    """
    candidates = ["go", "/usr/local/go/bin/go",
                  str(home_src / "go/bin/go"), str(home_src / ".local/bin/go")]
    binary = next((c for c in candidates if shutil.which(c)), None)
    if binary is None:
        return {}
    try:
        proc = subprocess.run([binary, "env", "-json"], capture_output=True, text=True,
                              timeout=15, check=False, env={**os.environ, "HOME": str(home_src)})
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}

    env: dict[str, str] = {}
    for key in GO_ENV_KEYS:
        val = str(data.get(key, "") or "").strip()
        if not val or val == "<nil>":
            continue
        env[key] = val.replace(str(home_src), str(home_dst))
    return env


def detect_condarc() -> str:
    path = invoking_home() / ".condarc"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "".join(f"      {line}" for line in text.splitlines(keepends=True))


def detect_npm_registry(home: Path) -> str:
    """nvm 裝的 npm 不在 root 的 PATH 上，必須直接找 nvm 的版本目錄。"""
    cands = ["npm", "/usr/local/bin/npm", "/usr/bin/npm"]
    nvm_bin = home / ".nvm" / "versions" / "node"
    if nvm_bin.is_dir():
        for ver in sorted(nvm_bin.iterdir(), reverse=True):
            cands.append(str(ver / "bin" / "npm"))
    for cand in cands:
        try:
            proc = subprocess.run([cand, "config", "get", "registry"], capture_output=True,
                                  text=True, timeout=15, check=False,
                                  env={**os.environ, "HOME": str(home)})
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        val = proc.stdout.strip()
        if proc.returncode == 0 and val.startswith("http"):
            return val
    return ""


def ensure_host_key(name: str) -> str:
    """宿主→客體的專用金鑰。不共用宿主的 ~/.ssh，避免宿主身分進入 VM。"""
    KEYS.mkdir(parents=True, exist_ok=True)
    key = KEYS / name
    pub = KEYS / f"{name}.pub"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"claude-vm:{name}",
                        "-f", str(key)], check=True, capture_output=True)
        os.chmod(key, 0o600)
    require(pub.is_file(), f"公鑰不存在：{pub}")
    return pub.read_text(encoding="utf-8").strip()


# 只接受 OpenSSH 認得的公鑰型別；`<type> <base64> [comment]`
PUBKEY_RE = re.compile(
    r"^(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r"\s+AAAA[0-9A-Za-z+/]+={0,2}(?:\s+\S.*)?$"
)


def scan_host_pubkeys(home: Path) -> list[str]:
    """掃宿主 ~/.ssh/*.pub，回傳可注入的授權公鑰（去重、去註解雜訊）。

    只讀公鑰檔，不碰私鑰；遇到解析不了的行直接略過，不讓壞檔中斷渲染。
    """
    ssh_dir = home / ".ssh"
    if not ssh_dir.is_dir():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(ssh_dir.glob("*.pub")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line or line.startswith("#"):
                continue
            if not PUBKEY_RE.match(line):
                continue
            blob = line.split(" ")[1]
            if blob in seen:
                continue
            seen.add(blob)
            out.append(line)
    return out


# ── 渲染 ────────────────────────────────────────────────────────────

def render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace(f"@@{key}@@", val)
    leftover = re.findall(r"@@[A-Z0-9_]+@@", text)
    if leftover:
        bad = next(l for l in text.splitlines() if "@@" in l)
        require(False, f"{template.name} 有未替換的佔位符：{sorted(set(leftover))}\n"
                       f"  首個出現於：{bad.strip()[:120]}")
    return text


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def drop_quic(cfg: dict, vm: dict) -> bool:
    """HTTP 代理無法承載 UDP，QUIC 必然失敗，主動 reject 讓它快速回退到 TCP。"""
    mode = str(cfg["gateway"].get("drop_quic", "auto")).lower()
    if mode in ("yes", "true", "always"):
        return True
    if mode in ("no", "false", "never"):
        return False
    require(mode == "auto", f"gateway.drop_quic 只接受 auto/yes/no，收到 {mode!r}")
    return parse_upstream(vm["upstream"])["type"] == "http"


def fstab_lines(mounts: list[dict]) -> str:
    used: set[str] = set()
    lines = []
    for i, m in enumerate(mounts):
        tag = mount_tag(m, used, f"mounts[{i}]")
        opts = "defaults,_netdev,nofail,x-systemd.device-timeout=10"
        if m.get("read_only"):
            opts += ",ro"
        lines.append(f"      {tag}  {m['target']}  virtiofs  {opts}  0 0")
    return "\n".join(lines)


def filesystems_xml(mounts: list[dict]) -> str:
    used: set[str] = set()
    out = []
    for i, m in enumerate(mounts):
        tag = mount_tag(m, used, f"mounts[{i}]")
        out.append(
            "    <filesystem type='mount' accessmode='passthrough'>\n"
            "      <driver type='virtiofs' queue='8'/>\n"
            f"      <source dir='{expand(m['source'])}'/>\n"
            f"      <target dir='{tag}'/>\n"
            "    </filesystem>"
        )
    return "\n".join(out)


def mkdir_lines(mounts: list[dict]) -> str:
    paths = " ".join(f"'{m['target']}'" for m in mounts)
    return f"mkdir -p {paths}"


def build_gateway_config(cfg: dict, vm: dict) -> dict:
    """該台 VM 的網關設定：TPROXY 入口 + 單一上游代理。

    沒有分流規則——上游是單節點直通，本專案選擇全量走節點。
    """
    gw = cfg["gateway"]
    upstream = parse_upstream(vm["upstream"])
    ports = vm["ports"]
    dns: dict = {
        "enable": True,
        "listen": f"0.0.0.0:{ports['dns']}",
        # redir-host 而非 fake-ip：避免客體 dig 得到 198.18.x.x 這個 Clash 指紋
        "enhanced-mode": "redir-host",
        "respect-rules": True,
        "nameserver": [str(gw["dns_upstream"])],
        # 上游代理自身的主機名必須直連解析，不能遞迴依賴自己
        "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
    }
    return {
        "mixed-port": 0,
        "tproxy-port": ports["tproxy"],
        # TCP 走 REDIRECT：mihomo 的 tproxy 監聽器綁在 IPv6 雙棧 socket 上，
        # 核心的 IPv4 TPROXY 查找匹配不到，封包會被丟棄。
        "redir-port": ports["redir"],
        "external-controller": ports["controller"],
        # TPROXY 的透明 socket 必須綁 0.0.0.0，否則只會接受發往 127.0.0.1 的封包。
        "allow-lan": True,
        # 必須是 IPv4 具體位址：mihomo 會把 "*" 解析成 [::]。
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "info",
        "dns": dns,
        "proxies": [upstream],
        "rules": ["MATCH,UPSTREAM"],
    }


def nft_fragments(cfg: dict) -> dict[str, str]:
    """per-VM 的 nft 規則片段。分派靠 `ip saddr`。"""
    bridge = cfg["network"]["bridge"]
    dns, redir, tproxy, quic = [], [], [], []
    for vm in cfg["vms"]:
        ip, p = vm["ip"], vm["ports"]
        if drop_quic(cfg, vm):
            # 上游是 HTTP 代理，無法承載 UDP，主動 reject 讓 QUIC 快速回退到 TCP
            quic.append(f'        iifname "{bridge}" ip saddr {ip} '
                        f'meta l4proto udp th dport 443 reject')
        dns.append(f'        iifname "{bridge}" ip saddr {ip} meta l4proto {{ tcp, udp }} '
                   f'th dport 53 redirect to :{p["dns"]}')
        redir.append(f'        iifname "{bridge}" ip saddr {ip} meta l4proto tcp '
                     f'redirect to :{p["redir"]}')
        tproxy.append(f'        iifname "{bridge}" ip saddr {ip} meta l4proto udp '
                      f'tproxy ip to :{p["tproxy"]} meta mark set 0x1 accept')
    ports = sorted({p[k] for vm in cfg["vms"] for k in ("redir", "tproxy", "dns") for p in [vm["ports"]]})
    return {
        "DNS_RULES": "\n".join(dns),
        "REDIR_RULES": "\n".join(redir),
        "TPROXY_RULES": "\n".join(tproxy),
        "QUIC_RULES": "\n".join(quic) if quic else "        # 所有上游皆支援 UDP，QUIC 直通",
        "GATEWAY_PORTS": ", ".join(str(p) for p in ports),
    }


def dhcp_hosts(cfg: dict) -> str:
    return "\n".join(
        # For a rotating MAC, dnsmasq identifies the IPv4 client by the
        # hostname supplied by cloud-init instead.  The IP remains stable, so
        # nft's per-VM source-IP routing remains intact.
        (f"      <host name='{vm['hostname']}' ip='{vm['ip']}'/>"
         if vm["mac_mode"] == "random" else
         f"      <host mac='{vm['mac']}' name='{vm['hostname']}' ip='{vm['ip']}'/>")
        for vm in cfg["vms"]
    )


def global_values(cfg: dict) -> dict[str, str]:
    net, gw, guest = cfg["network"], cfg["gateway"], cfg["guest"]
    subnet = ipaddress.ip_network(net["subnet"])
    return {
        "NET_NAME": str(net["name"]),
        "BRIDGE": str(net["bridge"]),
        "BRIDGE_MAC": str(net["bridge_mac"]).lower(),
        "GATEWAY": str(net["gateway"]),
        "NETMASK": str(subnet.netmask),
        "SUBNET_CIDR": str(subnet),
        "DHCP_START": str(net["dhcp_start"]),
        "DHCP_END": str(net["dhcp_end"]),
        "DHCP_HOSTS": dhcp_hosts(cfg),
        "HOST_LAN": str(net["host_lan"]),
        "GUEST_USER": str(guest["user"]),
        "GUEST_UID": str(int(guest["uid"])),
        "GUEST_TZ": str(guest.get("timezone", "UTC")),
        "HOST_SVC": ", ".join(str(int(p)) for p in gw["host_services"]),
        "LOCALFWD_PORTS": ", ".join(str(int(p)) for p in gw["host_services"]),
        **nft_fragments(cfg),
    }


def vm_values(cfg: dict, vm: dict) -> dict[str, str]:
    guest = cfg["guest"]
    home_host = invoking_home()
    home_guest = Path("/home") / str(guest["user"])
    go_env = detect_go_env(home_host, home_guest)
    npm_registry = detect_npm_registry(home_host)
    condarc = detect_condarc()
    name = vm["name"]

    git_name, git_email = name, f"{name}@claude-vm.local"

    # 管理金鑰永遠注入；guest.ssh_keys=scan 時再把宿主 ~/.ssh/*.pub 一併注入
    extra_keys = ([] if str(guest.get("ssh_keys", "scan")) == "none"
                  else scan_host_pubkeys(home_host))

    return {
        "VM_NAME": name,
        "VM_MAC": vm["mac"],
        "VM_MAC_MODE": vm["mac_mode"],
        "VM_UUID": vm["uuid"],
        "VM_IP": vm["ip"],
        "VCPU": str(vm["vcpu"]),
        "MEM_GB": str(vm["memory_gb"]),
        "DISK_SIZE_GB": str(vm["disk_size_gb"]),
        "DISK_PATH": str(expand(cfg["vm_defaults"]["disk_dir"]) / f"{name}.qcow2"),
        "SEED_PATH": str(expand(cfg["vm_defaults"]["disk_dir"]) / f"{name}-seed.iso"),
        "GUEST_HOSTNAME": vm["hostname"],
        "GUEST_TZ": vm["timezone"],
        "SSH_KEY_COMMENT": f"{guest['user']}@{vm['hostname']}",
        "SSH_AUTHORIZED_KEYS": "\n".join(
            f"      - {k}" for k in [ensure_host_key(name), *extra_keys]),
        "FILESYSTEMS_XML": filesystems_xml(vm["mounts"]),
        "FSTAB_LINES": fstab_lines(vm["mounts"]),
        "MOUNT_MKDIR": mkdir_lines(vm["mounts"]),
        "GO_ENV_FILE": "      # 宿主未偵測到 Go，略過\n" if not go_env
                       else "\n".join(f"      {k}={v}" for k, v in sorted(go_env.items())) + "\n",
        "GIT_CONFIG": (f"      [user]\n      \tname = {git_name}\n"
                       f"      \temail = {git_email}\n"),
        "NPMRC": (f"      registry={npm_registry}\n" if npm_registry
                  else "      # 宿主未設定 npm registry，使用預設\n"),
        "CONDARC": condarc or "      # 宿主無 .condarc\n",
    }


def scope_out(out: Path, scope: str) -> Path:
    """只清掉本次要重寫的子樹，避免渲染第二台時清掉第一台。"""
    path = out / ("shared" if scope == "shared" else f"vms/{scope}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def render_shared(cfg: dict, out: Path) -> None:
    dest = scope_out(out, "shared")
    values = global_values(cfg)
    for src, dst, mode in [
        ("network.xml.tmpl", "network.xml", 0o644),
        ("approx-gw.nft.tmpl", "approx-gw.nft", 0o644),
        ("approx-gw.sysctl.tmpl", "approx-gw.sysctl", 0o644),
        ("approx-gateway@.service.tmpl", "systemd/approx-gateway@.service", 0o644),
    ]:
        write(dest / dst, render(TEMPLATES / src, values), mode)
    for name in ("approx-gateway-net.service", "approx-gateway-net"):
        shutil.copy2(TEMPLATES / name, dest / "systemd" / name)
    os.chmod(dest / "systemd" / "approx-gateway-net", 0o755)


def render_vm(cfg: dict, vm: dict, out: Path) -> None:
    dest = scope_out(out, vm["name"])
    values = {**global_values(cfg), **vm_values(cfg, vm)}
    for src, dst, mode in [
        ("domain.xml.tmpl", "domain.xml", 0o644),
        ("cloud-init/user-data.yaml.tmpl", "cloud-init/user-data.yaml", 0o600),
        ("cloud-init/meta-data.tmpl", "cloud-init/meta-data", 0o644),
    ]:
        write(dest / dst, render(TEMPLATES / src, values), mode)
    write(dest / "mihomo" / "config.yaml",
          yaml.safe_dump(build_gateway_config(cfg, vm), sort_keys=False, allow_unicode=True), 0o600)


def env_values(cfg: dict, scope: str | None) -> dict[str, str]:
    net, gw = cfg["network"], cfg["gateway"]
    base = {
        "NET_NAME": str(net["name"]),
        "BRIDGE": str(net["bridge"]),
        "GATEWAY": str(net["gateway"]),
        "SUBNET_CIDR": str(ipaddress.ip_network(net["subnet"])),
        "HOST_LAN": str(net["host_lan"]),
        "GUEST_USER": str(cfg["guest"]["user"]),
        "CORE_BIN": str(expand(gw["core_bin"])),
        "DISK_DIR": str(expand(cfg["vm_defaults"]["disk_dir"])),
        "IMAGE_URL": str(cfg["vm_defaults"]["image_url"]),
        "VM_NAMES": " ".join(vm["name"] for vm in cfg["vms"]),
    }
    if scope is None or scope == "shared":
        return base
    vm = next((v for v in cfg["vms"] if v["name"] == scope), None)
    require(vm is not None, f"找不到 VM：{scope}")
    upstream = parse_upstream(vm["upstream"])
    return {
        **base,
        "VM_NAME": vm["name"],
        "VM_MAC": vm["mac"],
        "VM_MAC_MODE": vm["mac_mode"],
        "GUEST_HOSTNAME": vm["hostname"],
        "GUEST_TZ": vm["timezone"],
        "VM_UUID": vm["uuid"],
        "VM_IP": vm["ip"],
        "VCPU": str(vm["vcpu"]),
        "MEM_GB": str(vm["memory_gb"]),
        "DISK_SIZE_GB": str(vm["disk_size_gb"]),
        "DISK_PATH": str(expand(cfg["vm_defaults"]["disk_dir"]) / f"{vm['name']}.qcow2"),
        "SEED_PATH": str(expand(cfg["vm_defaults"]["disk_dir"]) / f"{vm['name']}-seed.iso"),
        "REDIR_PORT": str(vm["ports"]["redir"]),
        "TPROXY_PORT": str(vm["ports"]["tproxy"]),
        "DNS_PORT": str(vm["ports"]["dns"]),
        "CONTROLLER": vm["ports"]["controller"],
        "UPSTREAM_HOST": str(upstream["server"]),
        "UPSTREAM_PORT": str(upstream["port"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--out", default=str(ROOT / ".state" / "rendered"))
    ap.add_argument("--only", default=None, help="shared 或某台 VM 的名稱；省略則全部渲染")
    ap.add_argument("--check", action="store_true", help="只驗證，不寫檔")
    ap.add_argument("--env", action="store_true", help="輸出 shell 變數賦值")
    ap.add_argument("--list", action="store_true", help="列出 VM 名稱")
    ap.add_argument("--mounts", action="store_true", help="列出每台 VM 的共用掛載來源")
    args = ap.parse_args()

    try:
        cfg = normalise(load_config(Path(args.config)))
        validate(cfg)
        if args.env:
            for name, val in env_values(cfg, args.only).items():
                print(f"export {name}={shlex.quote(val)}")
            return 0
        if args.list:
            for vm in cfg["vms"]:
                print(f"{vm['name']}\t{vm['ip']}\t{vm['upstream']}")
            return 0
        if args.mounts:
            for vm in cfg["vms"]:
                for m in vm["mounts"]:
                    print(f"{vm['name']}\t{expand(m['source'])}\t{m.get('tag', '-')}")
            return 0
        if args.check:
            print(f"config OK: {len(cfg['vms'])} vms, "
                  f"{sum(len(v['mounts']) for v in cfg['vms'])} mounts, "
                  f"{len(cfg['gateway']['host_services'])} host services")
            for vm in cfg["vms"]:
                up = parse_upstream(vm["upstream"])
                print(f"  {vm['name']}: {vm['ip']} mac={vm['mac']} tz={vm['timezone']} "
                      f"ports={vm['ports']['redir']}/{vm['ports']['tproxy']}/{vm['ports']['dns']} "
                      f"upstream={up['type']}://{up['server']}:{up['port']}")
            return 0
    except (ConfigError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        if args.only in (None, "shared"):
            render_shared(cfg, out)
        if args.only is None:
            for vm in cfg["vms"]:
                render_vm(cfg, vm, out)
        elif args.only != "shared":
            vm = next((v for v in cfg["vms"] if v["name"] == args.only), None)
            require(vm is not None, f"找不到 VM：{args.only}")
            render_vm(cfg, vm, out)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # sudo 下渲染的產物會是 root 擁有，非特權指令之後讀不到
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        import pwd
        try:
            ent = pwd.getpwnam(os.environ["SUDO_USER"])
            for path in [out, *out.rglob("*"), KEYS, *KEYS.glob("*")]:
                if path.exists():
                    os.chown(path, ent.pw_uid, ent.pw_gid)
        except KeyError:
            pass

    print(f"rendered -> {out}  ({args.only or 'all'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
