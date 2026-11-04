# 帳號隔離：讓新帳號不繼承舊帳號的痕跡

前提：帳號的取得與合規由公司走正規渠道處理。本文件只解決一件事——
**在同一個宿主上，讓每一台 VM 的 Claude Code 身分互不相干，並且能證明它不相干。**

相關背景見 [`claude-identity.md`](claude-identity.md)（靜態分析）與
[`identity-channels.md`](identity-channels.md)（遙測以外的四條識別通道）。

---

## 1. 威脅模型：痕跡是怎麼串起來的

伺服器端不需要遥測就能把兩個帳號連起來，靠的是客戶端自己送出的東西：

| 痕跡 | 從哪來 | 換 VM 會消失？ | 換帳號會消失？ |
|------|--------|---------------|---------------|
| `metadata.user_id.device_id` | `~/.claude.json` 的 `userID` | 會（新檔新值） | **不會**（同一台機器固定） |
| `metadata.user_id.account_uuid` | 登入後的 `oauthAccount.accountUuid` | 會 | 會 |
| `~/.claude/.credentials.json` | OAuth 登入 | 會 | 會 |
| `~/.claude/projects/*.jsonl` | 歷史 session 全文 | 會 | **不會**（除非清掉） |
| `~/.claude.json.backup` | 舊設定快照 | 會 | **不會** |
| shell 歷史 | `~/.bash_history` 等 | 會 | **不會** |
| system prompt 內的 cwd／git／CLAUDE.md | 當次工作目錄 | 看掛載 | 看掛載 |
| 出口 IP、時區 | 網關設定 | 看設定 | 看設定 |
| commit 作者 | `~/.gitconfig` | 每台一份 | 每台一份 |

兩個關鍵事實：

1. **`device_id` 綁的是「這台機器的 `~/.claude.json`」，不是帳號。** 同一台 VM 換帳號登入，
   `device_id` 不變、`account_uuid` 變，兩個值會出現在同一個 `metadata.user_id` 物件裡。
2. **反過來，一台從未登入過的全新 VM，`userID` 是本機隨機產生的**，與任何舊帳號無關。

所以「一帳號一 VM、舊 VM 不再用」在客戶端這一側就是完備的；剩下的問題是「怎麼保證沒有例外」。

## 2. 倉庫已經提供的不隔離保證

| 機制 | 位置 | 效果 |
|------|------|------|
| per-VM `machine-id` | `lib/render.py: derive_uuid()` + cloud-init | 每台取自各自的 SMBIOS UUID，不共用 |
| per-VM hostname | `config.yaml: hostname` | `claude-vm` / `claude-vm-2`，不含人名 |
| 客體使用者固定 `claude` | `guest.user` | 路徑是 `/home/claude`，不洩漏真實帳號名 |
| per-VM git 身分 | `render.py: vm_values()` | `<vm>@claude-vm.local`，不吃宿主 `user.email` |
| per-VM SSH 管理金鑰 | `.state/keys/<vm>` → `/etc/ssh/authorized_keys.d/` | 每台一把管理金鑰；`guest.ssh_keys: scan` 時另注入宿主 `~/.ssh/*.pub`。放 `/etc` 是因為 `~/.ssh` 可能被共用掛載覆蓋 |
| per-VM 工作目錄 | `mounts` | `~/workspace` 與 `~/workspace-2` 各自獨立 |
| per-VM 時區與出口 | `vms[].timezone` / `upstream` | 出口 IP 不同 |
| 掛載黑名單 | `render.py: DENY_MOUNTS` | `~/.claude`、`~/.aws`、`.netrc`… 及其祖先／子孫目錄一律拒絕。**`~/.ssh` 是例外**：D15 起允許整份共用，SSH 身分因此不是獨立的 |

`bin/claude-vm verify` 會比對跨 VM 的出口 IP 與 `machineID`。

## 3. 本次新增的三項

### 3.1 `DENY_MOUNTS` 擴充

新增 `~/.claude.json`、`~/.config/claude-code`、`~/.local/share/claude`、`~/.codex`。
其中 `~/.claude.json` 是關鍵：它是 `device_id` 的載體，單獨掛進去等於把 VM 綁到宿主的身分。

### 3.2 `bin/claude-vm audit [name...]`（只讀）

盤點五件事，任何一項不通過就以非零離開：

1. **指紋表**：每台的 `oauth_email`、`userID`、`machineID`、版本、`.claude` 大小、session 數、歷史行數。
2. **跨 VM 重複**：`user_id`、`machine_uid`、`oauth_email`、`machine_id`、`hostname` 任一同值即失敗。
   其中 `oauth_email` 相同代表**兩個帳號共用一台機器**，是最直接的死亡條件。
3. **殘留字串**：若某台的 `~/.claude` 內出現**其他帳號**的 email，代表這台被跨帳號用過。
4. **宿主**：宿主家目錄是否有 Claude 身分（有憑證／有 email 就標紅）。
5. **共用掛載**：每個掛載來源的兩層內是否夾帶 `.claude.json`、`.credentials.json`、
   `.git-credentials`、`.netrc`。

### 3.3 `bin/claude-vm reset [name]`（破壞性）

把一台客體恢復成「從未登入過」：刪 `~/.claude`、`~/.claude.json`、`.backup`、
`~/.config/claude*`、`~/.cache/claude*`，清空 shell 歷史（bash／zsh／fish／PSReadLine）。
`--rotate-ssh` 重產客體金鑰（若 `~/.ssh` 由宿主掛載則跳過，避免刪到宿主金鑰），
`--scrub-git` 用 `/etc/claude-vm/gitconfig` 覆蓋 `~/.gitconfig`。
不動 workspace 掛載、工具鏈與磁碟映像。

next boot 時 Claude Code 會重新產生 `userID` 與 `machineID`。

## 4. 操作不變式

新帳號上線前，這四條必須成立：

1. **一個帳號一台 VM。** 新帳號用 `bin/claude-vm up <new-name>` 從映像新建，不要 `reset` 舊 VM 後沿用
   ——`reset` 是給「這台要換用途／換人」用的，不是給「同一批人開第二個帳號」用的。
2. **宿主永不在新帳號下執行 `claude`。** 宿主的 `~/.claude.json` 屬於另一台裝置；
   一旦在宿主登入，`device_id` 立刻與 VM 們無關的第三個裝置綁上。
3. **workspace 不跨帳號共用。** 每台 VM 掛自己的目錄。若必須共用，至少確認裡面沒有
   `.claude.json`／`.credentials.json`，且 commit 作者不是宿主身分（`git log --format='%ae' | sort -u`）。
4. **上線後跑 `audit`，必須全綠**；換帳號或移交前跑 `reset`，再跑一次 `audit`。

## 5. 遙測與非必要流量（已內建於 cloud-init）

遙測關掉只影響識別通道裡最外圍的一層；`metadata.user_id` 照送。從現在起這是**預設值**，
不需要手動設定：cloud-init 會寫兩個地方，兩者內容一致。

`/etc/claude-vm/claude-settings.json` → 安裝為 `~/.claude/settings.json`：

```json
{
  "env": {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    "DISABLE_BUG_COMMAND": "1"
  },
  "cleanupPeriodDays": 7
}
```

`/etc/profile.d/claude-vm-telemetry.sh` 匯出同一組變數，覆蓋不讀 settings.json 的啟動路徑。

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 是總開關，會一併關掉 feature flag 評估，
Remote Control 因此不可用——這是刻意的取捨。

`cleanupPeriodDays` 直接決定 `~/.claude/projects/*.jsonl` 留多久（預設 30 天）；
7 天讓跨帳號殘留的視窗更小。

## 6. 檢查表

開新帳號當天：

```bash
bin/claude-vm up claude-vm-3            # 全新 VM，新 userID／machineID
bin/claude-vm audit                     # 應顯示 3 台、無重複、宿主警示可接受
# 客體內登入新帳號
bin/claude-vm audit                     # 再跑一次，oauth_email 只出現在這一台
```

移交或停用時：

```bash
bin/claude-vm reset <name> --yes --rotate-ssh --scrub-git
bin/claude-vm audit <name>
```

## 7. 明確做不到的事

- `audit` 只證明**客戶端**乾淨。伺服器手上的 IP、支付、KYC、內容指紋，它看不到也管不到。
- 同一批私有 repo 的內容、同樣的 commit、同樣的檔案路徑，換幾台 VM 都一樣會被關聯。
  這不是客戶端能修的，也不該假裝能修。
- **`/etc/machine-id` 重建後不變。** 它由 VM 名稱派生（`render.py: derive_uuid()`），
  所以 `claude-vm` 重新建幾次都是同一個值。它只出現在 OTEL 的 `host.id`，
  遙測關閉後不會送出；真要換掉它，得改 VM 名稱或讓 UUID 隨機（會失去「同一台的穩定識別」）。
- 本文件不提供「讓一台機器看起來像另一台」的手段（偽造 KYC、支付、硬體指紋）。
  那些屬於 [Usage Policy](https://www.anthropic.com/legal/aup) 明文禁止的範圍。
