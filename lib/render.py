#!/usr/bin/env python3
"""從 config.yaml + templates/ 渲染部署產物。

本模組不做任何系統變更：只讀設定，寫出檔案到 --out。
virsh / nft / systemctl 由 bin/claude-vm 負責。

代理本身不由本倉庫管理：設定只描述「上游代理位址」，
閘道負責把 VM 流量送到那裡。分流規則在上游。
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
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


def load_config(path: Path) -> dict:
    require(path.is_file(), f"設定檔不存在：{path}（先執行 claude-vm init）")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(cfg, dict), "設定檔必須是 YAML mapping")
    for section in ("vm", "network", "gateway", "guest", "mounts"):
        require(section in cfg, f"設定檔缺少 {section} 區段")
    return cfg


def detect_go_env(home_src: Path, home_dst: Path) -> dict[str, str]:
    """讀取宿主 `go env`，把需要在客體重現的設定挑出來。

    找不到 go、或執行失敗時回傳空字典——呼叫端據此跳過。
    """
    # sudo 的 secure_path 不含 /usr/local/go/bin，直接呼叫 "go" 會找不到。
    # 依序嘗試 PATH 與常見安裝位置。
    candidates = ["go",
                  "/usr/local/go/bin/go",
                  str(home_src / "go/bin/go"),
                  str(home_src / ".local/bin/go")]
    binary = next((c for c in candidates if shutil.which(c)), None)
    if binary is None:
        return {}
    try:
        # HOME 必須指向呼叫者的家目錄，否則 root 會讀 /root/.config/go/env，
        # 拿到的設定與使用者實際使用的不一致（例如 GOPROXY 鏡像）。
        proc = subprocess.run(
            [binary, "env", "-json"],
            capture_output=True, text=True, timeout=15, check=False,
            env={**os.environ, "HOME": str(home_src)},
        )
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
        # 家目錄路徑改寫成客體的使用者目錄
        env[key] = val.replace(str(home_src), str(home_dst))
    return env


def detect_git_identity(home: Path) -> dict[str, str]:
    """讀取宿主的 git 身分，讓客體能直接 commit。"""
    out = {}
    for key, cfg in (("name", "user.name"), ("email", "user.email")):
        try:
            proc = subprocess.run(["git", "config", "--global", cfg],
                                  capture_output=True, text=True, timeout=10, check=False,
                                  env={**os.environ, "HOME": str(home)})
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return {}
        val = proc.stdout.strip()
        if proc.returncode != 0 or not val:
            return {}
        out[key] = val
    return out


def detect_condarc() -> str:
    """讀取宿主的 conda 設定（通常是國內鏡像），客體沿用。"""
    path = invoking_home() / ".condarc"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    # YAML 區塊純量內需要縮排
    return "".join(f"      {line}" for line in text.splitlines(keepends=True))


def detect_npm_registry(home: Path) -> str:
    """讀取宿主的 npm registry（通常是國內鏡像），客體沿用。

    nvm 裝的 npm 不在 root 的 PATH 上，必須直接找 nvm 的版本目錄。
    """
    cands = ["npm", "/usr/local/bin/npm", "/usr/bin/npm"]
    nvm_bin = home / ".nvm" / "versions" / "node"
    if nvm_bin.is_dir():
        # 取版本號最大的一個
        for ver in sorted(nvm_bin.iterdir(), reverse=True):
            cands.append(str(ver / "bin" / "npm"))
    for cand in cands:
        try:
            proc = subprocess.run([cand, "config", "get", "registry"],
                                  capture_output=True, text=True, timeout=15, check=False,
                                  env={**os.environ, "HOME": str(home)})
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        val = proc.stdout.strip()
        if proc.returncode == 0 and val.startswith("http"):
            return val
    return ""


def go_env_file(env: dict[str, str]) -> str:
    """Go 的環境檔格式：每行 KEY=VALUE。"""
    if not env:
        return "      # 宿主未偵測到 Go，略過\n"
    body = "\n".join(f"      {k}={v}" for k, v in sorted(env.items()))
    return body + "\n"


def parse_upstream(url: str) -> dict:
    """把上游代理位址解析成 mihomo 的 proxy 條目。"""
    u = urlparse(url)
    require(u.scheme in PROXY_SCHEMES,
            f"gateway.upstream 只支援 {sorted(PROXY_SCHEMES)}，收到 {u.scheme!r}")
    require(bool(u.hostname), f"gateway.upstream 缺少主機名：{url}")
    require(u.port is not None, f"gateway.upstream 缺少埠號：{url}")
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


def validate(cfg: dict) -> None:
    vm, net, gw, guest = cfg["vm"], cfg["network"], cfg["gateway"], cfg["guest"]

    require(re.fullmatch(r"[a-z][a-z0-9-]{1,30}", str(vm["name"])) is not None,
            f"vm.name 必須是 DNS label：{vm['name']}")
    require(int(vm["vcpu"]) >= 1, "vm.vcpu 必須 >= 1")
    require(int(vm["memory_gb"]) >= 2, "vm.memory_gb 必須 >= 2")
    require(int(vm["disk_size_gb"]) >= 10, "vm.disk_size_gb 必須 >= 10")

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

    parse_upstream(str(gw["upstream"]))

    dns_upstream = str(gw.get("dns_upstream", ""))
    require(dns_upstream.startswith(("https://", "tls://", "tcp://", "udp://")),
            "gateway.dns_upstream 必須是 https:// tls:// tcp:// 或 udp:// 位址")

    for key in ("tproxy_port", "dns_port", "redir_port"):
        port = int(gw[key])
        require(1 <= port <= 65535, f"gateway.{key} 超出範圍：{port}")
    ports = [int(gw["tproxy_port"]), int(gw["dns_port"]), int(gw["redir_port"])]
    require(len(set(ports)) == 3, "tproxy_port / dns_port / redir_port 不可重複")

    services = [int(p) for p in gw["host_services"]]
    require(all(1 <= p <= 65535 for p in services), "gateway.host_services 含無效端口")
    require(len(set(services)) == len(services), "gateway.host_services 有重複端口")
    require(not set(services) & set(ports), "host_services 不可與代理端口衝突")

    require(int(guest["uid"]) > 0, "guest.uid 不可為 0（root）")

    for i, m in enumerate(cfg["mounts"]):
        src = expand(m["source"])
        require(src.is_dir(), f"mounts[{i}].source 不是目錄：{src}")
        require(str(m["target"]).startswith("/"), f"mounts[{i}].target 必須是絕對路徑")


def mount_tag(m: dict, used: set[str]) -> str:
    """virtiofs 的 target dir 是標籤不是路徑，必須唯一且穩定。"""
    if m.get("tag"):
        tag = TAG_SAFE.sub("-", str(m["tag"]).lower()).strip("-")
        require(bool(tag), f"mounts tag 無效：{m['tag']}")
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


def build_gateway_config(cfg: dict) -> dict:
    """產生網關的 mihomo 設定：TPROXY 入口 + 單一上游代理。

    沒有分流規則——分流在上游做。這裡只負責把封包送出去。
    """
    gw = cfg["gateway"]
    upstream = parse_upstream(str(gw["upstream"]))

    dns: dict = {
        "enable": True,
        "listen": f"0.0.0.0:{int(gw['dns_port'])}",
        # redir-host 而非 fake-ip：避免客體 dig 得到 198.18.x.x 這個 Clash 指紋
        "enhanced-mode": "redir-host",
        # 讓 DNS 查詢也走規則（= 走上游代理），避免解析結果洩漏真實出口
        "respect-rules": True,
        "nameserver": [str(gw["dns_upstream"])],
        # 上游代理自身的主機名必須直連解析，不能遞迴依賴自己
        "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
    }

    return {
        "mixed-port": 0,
        "tproxy-port": int(gw["tproxy_port"]),
        # TCP 走 REDIRECT：mihomo 的 tproxy 監聽器綁在 IPv6 雙棧 socket 上，
        # 核心的 IPv4 TPROXY 查找匹配不到，封包會被丟棄。
        "redir-port": int(gw["redir_port"]),
        "external-controller": str(gw["controller"]),
        # TPROXY 的透明 socket 必須綁 0.0.0.0，否則只會接受發往 127.0.0.1 的封包。
        # allow-lan 為 false 時 mihomo 會把所有監聽器收斂到 loopback，所以必須開。
        # 對外暴露的風險由 nft 的 input 鏈擋住（見 templates/approx-gw.nft.tmpl）。
        "allow-lan": True,
        # 必須是 IPv4 具體位址：mihomo 會把 "*" 解析成 [::]，
        # 而 IPv4 的 TPROXY 需要一個真正的 IPv4 socket 才收得到封包。
        "bind-address": "0.0.0.0",
        "mode": "rule",
        "log-level": "info",
        "dns": dns,
        "proxies": [upstream],
        "rules": ["MATCH,UPSTREAM"],
    }


def drop_quic(cfg: dict) -> bool:
    """HTTP 代理無法承載 UDP，QUIC 必然失敗。

    主動 reject 讓瀏覽器立刻回退到 TCP，而不是等到逾時。
    """
    mode = str(cfg["gateway"].get("drop_quic", "auto")).lower()
    if mode in ("yes", "true", "always"):
        return True
    if mode in ("no", "false", "never"):
        return False
    require(mode == "auto", f"gateway.drop_quic 只接受 auto/yes/no，收到 {mode!r}")
    return parse_upstream(str(cfg["gateway"]["upstream"]))["type"] == "http"


def fstab_lines(cfg: dict, tags: dict[int, str]) -> str:
    lines = []
    for i, m in enumerate(cfg["mounts"]):
        opts = "defaults,_netdev,nofail,x-systemd.device-timeout=10"
        if m.get("read_only"):
            opts += ",ro"
        lines.append(f"      {tags[i]}  {m['target']}  virtiofs  {opts}  0 0")
    return "\n".join(lines)


def filesystems_xml(cfg: dict, tags: dict[int, str]) -> str:
    return "\n".join(
        "    <filesystem type='mount' accessmode='passthrough'>\n"
        "      <driver type='virtiofs' queue='8'/>\n"
        f"      <source dir='{expand(m['source'])}'/>\n"
        f"      <target dir='{tags[i]}'/>\n"
        "    </filesystem>"
        for i, m in enumerate(cfg["mounts"])
    )


def mkdir_lines(cfg: dict) -> str:
    paths = " ".join(f"'{m['target']}'" for m in cfg["mounts"])
    return f"mkdir -p {paths}"


def ssh_key(cfg: dict) -> str:
    """讀取客體使用者的 SSH 公鑰。只接受公鑰，避免誤把私鑰寫進映像。"""
    path = expand(cfg["guest"]["ssh_authorized_key_file"])
    require(path.is_file(), f"SSH 公鑰不存在：{path}")
    key = path.read_text(encoding="utf-8").strip()
    require(key.startswith(("ssh-", "ecdsa-", "sk-")), f"{path} 不像 SSH 公鑰")
    require("PRIVATE KEY" not in key, f"{path} 是私鑰，拒絕使用")
    require("\n" not in key, f"{path} 含多行內容，請只放一把公鑰")
    return key


def render(template: Path, values: dict[str, str]) -> str:
    text = template.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace(f"@@{key}@@", val)
    leftover = re.findall(r"@@[A-Z0-9_]+@@", text)
    if leftover:
        bad = next(l for l in text.splitlines() if "@@" in l)
        require(False, f"{template.name} 有未替換的佔位符：{sorted(set(leftover))}\n  首個出現於：{bad.strip()[:120]}")
    return text


def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--out", default=str(ROOT / ".state" / "rendered"))
    ap.add_argument("--check", action="store_true", help="只驗證，不寫檔")
    ap.add_argument("--env", action="store_true", help="輸出 shell 變數賦值，供 bin/claude-vm eval")
    args = ap.parse_args()

    try:
        cfg = load_config(Path(args.config))
        validate(cfg)
        key = ssh_key(cfg)
    except (ConfigError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    vm, net, gw, guest = cfg["vm"], cfg["network"], cfg["gateway"], cfg["guest"]
    subnet = ipaddress.ip_network(net["subnet"])
    disk_dir = expand(vm["disk_dir"])
    upstream = parse_upstream(str(gw["upstream"]))

    env_values = {
        "VM_NAME": str(vm["name"]),
        "VCPU": str(int(vm["vcpu"])),
        "MEM_GB": str(int(vm["memory_gb"])),
        "DISK_DIR": str(disk_dir),
        "DISK_PATH": str(disk_dir / f"{vm['name']}.qcow2"),
        "SEED_PATH": str(disk_dir / f"{vm['name']}-seed.iso"),
        "IMAGE_URL": str(vm["image_url"]),
        "DISK_SIZE_GB": str(int(vm["disk_size_gb"])),
        "NET_NAME": str(net["name"]),
        "BRIDGE": str(net["bridge"]),
        "GATEWAY": str(net["gateway"]),
        "SUBNET_CIDR": str(subnet),
        "HOST_LAN": str(net["host_lan"]),
        "GUEST_USER": str(guest["user"]),
        "CORE_BIN": str(expand(gw["core_bin"])),
        "UPSTREAM_HOST": str(upstream["server"]),
        "UPSTREAM_PORT": str(upstream["port"]),
        "TPROXY_PORT": str(int(gw["tproxy_port"])),
        "REDIR_PORT": str(int(gw["redir_port"])),
        "DNS_PORT": str(int(gw["dns_port"])),
        "CONTROLLER": str(gw["controller"]),
    }
    if args.env:
        import shlex
        for name, val in env_values.items():
            print(f"export {name}={shlex.quote(val)}")
        return 0

    used: set[str] = set()
    tags = {i: mount_tag(m, used) for i, m in enumerate(cfg["mounts"])}
    quic = drop_quic(cfg)

    host_home = invoking_home()
    guest_home = Path("/home") / str(guest["user"])
    go_env = detect_go_env(host_home, guest_home)
    git_identity = detect_git_identity(host_home)
    npm_registry = detect_npm_registry(host_home)
    condarc = detect_condarc()

    common = {
        **env_values,
        "FILESYSTEMS_XML": filesystems_xml(cfg, tags),
        "BRIDGE_MAC": str(net["bridge_mac"]),
        "NETMASK": str(subnet.netmask),
        "DHCP_START": str(net["dhcp_start"]),
        "DHCP_END": str(net["dhcp_end"]),
        "HOST_SVC": ", ".join(str(int(p)) for p in gw["host_services"]),
        "GUEST_UID": str(int(guest["uid"])),
        "GUEST_HOSTNAME": str(guest["hostname"]),
        "FSTAB_LINES": fstab_lines(cfg, tags),
        "MOUNT_MKDIR": mkdir_lines(cfg),
        "SSH_KEY": key,
        "GO_ENV_FILE": go_env_file(go_env),
        "GIT_NAME": git_identity.get("name", ""),
        "GIT_EMAIL": git_identity.get("email", ""),
        # 這兩個佔位符在模板中位於 YAML 區塊純量內，必須自行帶縮排
        "GIT_CONFIG": (
            f"      [user]\n      \tname = {git_identity['name']}\n"
            f"      \temail = {git_identity['email']}\n"
            if git_identity else "      # 宿主未設定 git 身分，略過\n"
        ),
        "NPMRC": (f"      registry={npm_registry}\n" if npm_registry
                  else "      # 宿主未設定 npm registry，使用預設\n"),
        "CONDARC": condarc or "      # 宿主無 .condarc\n",
        "LOCALFWD_PORTS": ", ".join(str(int(p)) for p in gw["host_services"]),
        "QUIC_RULE": (
            "        # 上游是 HTTP 代理，無法承載 UDP，主動 reject 讓 QUIC 快速回退到 TCP\n"
            "        meta l4proto udp th dport 443 reject"
            if quic else
            "        # 上游支援 UDP，QUIC 直通"
        ),
    }

    out = Path(args.out)
    if args.check:
        print(f"config OK: {len(cfg['mounts'])} mounts, "
              f"{len(gw['host_services'])} host services, "
              f"upstream={upstream['type']}://{upstream['server']}:{upstream['port']}, "
              f"drop_quic={quic}, go_env={len(go_env)} keys, "
              f"git={'yes' if git_identity else 'no'}, npm={npm_registry or 'default'}")
        return 0

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    plan = [
        ("network.xml.tmpl", "network.xml", 0o644),
        ("domain.xml.tmpl", "domain.xml", 0o644),
        ("approx-gw.nft.tmpl", "approx-gw.nft", 0o644),
        ("approx-gw.sysctl.tmpl", "approx-gw.sysctl", 0o644),
        ("approx-gateway.service.tmpl", "systemd/approx-gateway.service", 0o644),
        ("cloud-init/user-data.yaml.tmpl", "cloud-init/user-data.yaml", 0o600),
        ("cloud-init/meta-data.tmpl", "cloud-init/meta-data", 0o644),
    ]
    for src, dst, mode in plan:
        write(out / dst, render(TEMPLATES / src, common), mode)

    for name in ("approx-gateway-net.service", "approx-gateway-net"):
        shutil.copy2(TEMPLATES / name, out / "systemd" / name)
    os.chmod(out / "systemd" / "approx-gateway-net", 0o755)

    write(out / "mihomo" / "config.yaml",
          yaml.safe_dump(build_gateway_config(cfg), sort_keys=False, allow_unicode=True), 0o600)

    # sudo 下渲染的產物會是 root 擁有，非特權指令之後讀不到
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        import pwd
        try:
            ent = pwd.getpwnam(os.environ["SUDO_USER"])
            for path in [out, *out.rglob("*")]:
                os.chown(path, ent.pw_uid, ent.pw_gid)
        except KeyError:
            pass

    print(f"rendered -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
