# Claude Code 的用戶識別通道：遙測以外

分析對象：`2.1.251`（macOS arm64，Bun standalone）、`2.1.317`（Linux x86-64，Bun standalone）
方法：（1）`strings` 取二進位內嵌 JS 片段；（2）**動態攔截**——把 `ANTHROPIC_BASE_URL` 指向本機 loopback server，
以假 API key 跑 `claude -p`，記錄實際送出的 header 與 body（全程不連 Anthropic）。
證據標記：**[直證]** 二進位字串或攔截到的實際請求；**[推論]** 由欄位結構推得。

> 本文件是「客戶端到底送了什麼」的技術盤點，不是規避封禁的操作指南。
> 為什麼不往那個方向寫，見第 8 節。

---

## 0. 結論

遙測（telemetry）只是四條通道裡最容易被關掉的一條。真正把「人」綁起來的是另外三條：

| # | 通道 | 是否受遙測開關影響 | 內容 |
|---|------|------------------|------|
| 1 | **推理請求的 `metadata.user_id`** | **否** | `device_id` + `account_uuid` + `session_id`，每個 `/v1/messages` 都帶 |
| 2 | **推理請求的 system / context 內容** | **否** | cwd、平台、shell、OS、git 分支與 commit、CLAUDE.md、skills、MCP 工具、shell 歷史摘要 |
| 3 | **控制面 / 帳號面請求** | 部分 | `settings`、`policy_limits`、`usage`、GrowthBook、update feed；以 OAuth token 認證，天生綁帳號 |
| 4 | 遙測（event_logging / Sentry / Datadog / Segment） | 是 | 見第 5 節，可全關 |

第 1 條是關鍵：它走的是**推理路徑**，不是遙測路徑。把 `DISABLE_TELEMETRY`、
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`、`DISABLE_ERROR_REPORTING` 全部設起來之後，它依然照送。

---

## 1. `metadata.user_id`：每次推理都附帶的裝置—帳號綁定 [直證]

攔截到的實際 body（節錄，值已截斷）：

```json
{
  "model": "claude-sonnet-4-5",
  "messages": [...],
  "metadata": {
    "user_id": "{\"device_id\":\"<64 hex>\",\"account_uuid\":\"\",\"session_id\":\"<uuid>\"}"
  },
  "max_tokens": 32000
}
```

- `metadata.user_id` 是 Anthropic Messages API 的公開欄位，這裡被當成**複合識別碼的載體**：一個 JSON 字串，內含三個值。
- `device_id` **等於** `~/.claude.json` 裡全域的 `userID`（64 hex，本機首次執行以 `crypto.randomBytes(32)` 產生後持久化，不由系統特徵推導）。[直證：數值比對相符]
- `account_uuid` 在 API key 模式下是空字串；OAuth 登入模式應填入 `~/.claude.json` 的 `oauthAccount.accountUuid`。
  [推論：欄位同名、同一物件、且 `oauthAccount.accountUuid` 就存在本機]
- `session_id` 每次 session 隨機。

**意義**：同一台機器、同一個 `~/.claude.json`，換帳號登入 → `device_id` 不變、`account_uuid` 變。
對伺服器而言，這是一張現成的「同裝置 ↔ 多帳號」對照表，不需要任何遙測資料就能 join。
這正好對應「新帳號被秒封」的機制。

## 2. 推理請求的標頭 [直證]

實際送出（`2.1.251`）：

```
User-Agent: claude-cli/2.1.251 (external, sdk-cli)
x-app: cli                                  # 背景程序為 cli-bg
X-Claude-Code-Session-Id: <uuid>
X-Stainless-OS: MacOS
X-Stainless-Arch: arm64
X-Stainless-Runtime: node
X-Stainless-Runtime-Version: v26.3.0
X-Stainless-Package-Version: 0.112.1
X-Stainless-Retry-Count: 0
X-Stainless-Timeout: 600
anthropic-version: 2023-06-01
anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,
                thinking-token-count-2026-05-13,context-management-2025-06-27,
                prompt-caching-scope-2026-01-05,advisor-tool-2026-03-01,
                structured-outputs-2025-12-15
anthropic-dangerous-direct-browser-access: true
x-anthropic-billing-header: cc_version=2.1.251.a8c; cc_entrypoint=sdk-cli;
```

`x-anthropic-billing-header` 的完整欄位（靜態）[直證]：

```
cc_version=<ver>.<hash>; cc_entrypoint=<cli|sdk-cli|…>;
cch=00000;                 # prompt cache 相關雜湊
cc_workload=<…>;
cc_is_subagent=true;       # 子代理時
cc_prev_req=<req_id>;      # 串接前一個請求
cc_prompt_id=<uuid>;       # 僅 firstParty 且 oauth 情境
```

`cch` / `cc_prompt_id` / `cc_prev_req` 讓「同一段 prompt 在不同帳號出現」在伺服器端可被串起來。

## 3. 模型看到的系統上下文（不是 header，但一樣送出） [直證]

`2.1.251` 實際攔到的 `<system-reminder>`：

```
# Environment
You have been invoked in the following environment:
 - Primary working directory: /private/tmp/ccaudit/testrepo
 - Is a git repository: true
 - Additional working directories:
  - /private/tmp/ccaudit/testrepo
 - Platform: darwin
 - Shell: zsh
 - OS Version: Darwin 24.6.0
```

Git 情境另有一段 system context（靜態 [直證]，`-p` 模式未觸發，互動模式會）：

```
This is the git status at the start of the conversation. …
Current branch: <branch>
Main branch (you will usually use this for PRs): <branch>
Git user: <git config user.name>
Status:
<git status --short，上限 2000 字元>
Recent commits:
<git log --oneline -n 5>
```

- `Git user` 取的是 `git config user.name`，**不含** email [直證]。
- `Status` 是 `--short`，會把 repo 內的檔名／目錄結構送出去。
- `Recent commits` 是 oneline（含 commit message，不含作者）。commit message 由人寫，常含人名、工單號、客戶名。

連帶送出、且都會進 inference 的：`CLAUDE.md`、skills 清單、MCP server 與工具描述、agent 定義、
歷史 session 摘要。**檔案絕對路徑本身**就含使用者名稱（本機 `/Users/<name>/…`、伺服器 `/home/<name>/…`）。

## 4. 本機讀取面（「倉庫程式碼以外」的來源）

| 來源 | 讀什麼 | 用途 | 證據 |
|------|--------|------|------|
| `~/.claude.json` | `machineID`、`userID`、`oauthAccount`（email、accountUuid、organizationUuid、billingType、orgName、seatTier…）、`cachedGrowthBookFeatures`、專案清單 | 識別、功能旗標、狀態 | [直證] |
| git | `config user.name`、`branch`、`status --short`、`log --oneline -5`、remote（bug report 時） | 系統上下文 | [直證] |
| shell 歷史 | `$HISTFILE`、`~/.zsh_history`、`~/.bash_history`、PSReadLine、`~/.local/share/fish/fish_history` | auto-mode 設定分析（**只取 command words**） | [直證] |
| `$HOME` 下的 git repo | 目錄列舉、remote host、repo 名稱、sibling docs | 同上（`maxDirsVisited=4000`、`maxRepos=20`、`maxRepoDepth=2`） | [直證] |
| 設定／技能／外掛 | `settings.json`、`skills/`、`plugins/`、managed settings | 功能與提示 | [直證] |

有明確的**憑證檔案 deny-list**（設計上會跳過，不會讀進 prompt）[直證]：

```
\.netrc | \.gnupg/ | /etc/shadow | \.kube/config | \.docker/config\.json
\.npmrc | \.pypirc | \.git-credentials | \.config/gh/hosts\.yml | \.config/glab-cli/config\.yml
```

以及 secret 掃描 regex（`auth|key|token|cookie|secret|credential|session|signature|passw|jwt|assertion|cert|oidc`）。
另外有 `cc_automode` / `<settings_` 之類的模板 token 注入防護——這部分設計是認真的。

## 5. 遙測與第三方（這塊可以關）

Anthropic 自己在 `EGRESS_SERVICES` 表裡把對外服務分成四類 [直證]：

| 分類 | 端點 | 對應開關（settings） |
|------|------|---------------------|
| Core | `claude.ai`、`api.anthropic.com`（推理、OAuth、更新） | 無（必要） |
| Auto-updates | `claude.ai/claude-code-releases`、`/api/desktop/.../update` | `disableAutoUpdates` |
| **Essential telemetry** | `*.ingest.us.sentry.io`、`sentry.io`、`browser-intake-*-datadoghq.com` | `telemetry.disableEssential` |
| **Nonessential telemetry** | `segment`（CDN/API 常數；字串表未命中具體主機名）、`api.anthropic.com/api/event_logging/` | `telemetry.disableNonessential` |
| **Nonessential services** | `/mcp-registry/`、`/api/directory/`、favicon proxy、artifact sandbox | `telemetry.disableNonessentialServices` |

各自的 payload：

- **`/api/event_logging/v2/batch`**（第一方事件，batch ≤200）：`event_name`、`device_id`（= userID）、
  `session_id`、`email`、`account_uuid`、`organization_uuid`、`user_type`、`entrypoint`、`model`、
  `betas`、`is_interactive`、`agent_id`/`parent_session_id`/`agent_type`、`skill_name`、`plugin_name`、
  `marketplace_name`、`mcp_server_name`、`mcp_tool_name`、`head_sha`、`auth`、`process`、`env`。[直證]
- **Sentry**（錯誤回報）：user context = `deviceId` + `email` + `appVersion` + `account/organization_uuid`。[直證]
  官方文件：預設只在 Pro/Max/Ultra + v2.1.198+ + 直連 API + 無 ZDR/HIPAA 時開啟。
- **Datadog logs**：`https://http-intake.logs.us5.datadoghq.com/api/v2/logs`，公開 client token
  `pubea…`（`tengu_*` 事件）。[直證]
- **GrowthBook**：`before/after_growthbook_init`；實驗事件 `GrowthbookExperimentEvent` 同時帶
  `account_uuid`、`organization_uuid`、`device_id`、`session_id`、`user_attributes`。[直證]
  → 這是「同一 device 出現在兩個 account」的另一個 join 點。

關閉方式（官方文件 + 二進位 gate 一致）：

```bash
DISABLE_TELEMETRY=1                     # metrics（會一併關掉 feature flag evaluation）
DISABLE_ERROR_REPORTING=1               # Sentry，不影響 flag
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   # 一次關掉非必要流量
DO_NOT_TRACK=1
CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
DISABLE_AUTOUPDATER=1
DISABLE_FEEDBACK_COMMAND=1 / DISABLE_BUG_COMMAND=1
```

`settings.json` 亦可寫 `telemetry: { disableEssential, disableNonessential, disableNonessentialServices }`。

**關不掉的**：`/v1/messages` 本身（含第 1、2 節全部內容）、`/api/claude_code/settings`、
`/api/claude_code/policy_limits`、`/api/oauth/usage`、WebFetch 的網域安全檢查（連 hostname 送 `api.anthropic.com`）。

## 6. 客戶端之外：伺服器與網頁端看得到的

這些不在 Claude Code 裡，但在同一條判斷鏈上：

- IP、ASN、時區、語言、TLS/HTTP2 指紋（JA3/JA4）、連線時間規律。
- `claude.ai` 瀏覽器端：Cookie、裝置指紋、Cloudflare 風控、註冊流程的 CAPTCHA。
- **Persona KYC**：政府證件影像、臉部幾何模板、驗證結果（Privacy Policy 明列為 Verification Data）。
- 支付方式指紋（同一張卡／同一 BIN／同一帳單地址／退款紀錄）。
- **內容指紋**：同一批私有 repo、同樣的檔名與程式碼、同樣的 commit message，出現在兩個帳號的 inference 裡。
  這一條最難規避，因為它就是「你正在做的事」本身。

## 7. 法律文件：Anthropic 說它收什麼、禁止什麼

### 7.1 Privacy Policy 收集的類別

- 身分與聯絡資料：姓名、email、電話；「間接識別碼」（如 `USER12345`）。
- 支付資訊。
- **Inputs / Outputs**：含 agentic session 的內容、上傳的檔案、整合第三方服務取回的內容。
- Feedback（按讚／倒讚會連整段對話一起存）。
- **Verification Data**：身分證件影像與其上資訊、照片／影片、**臉部幾何模板**（部分法域視為生物辨識資料）、驗證結果。
- 自動取得的 Technical Information：裝置與連線資訊（裝置類型、OS、瀏覽器、referer、ISP、**時區**、
  **IP 與由 IP 推得的位置**、device／advertising／probabilistic identifiers）、使用資訊、
  日誌與疑難排解資訊、Cookie 與類似技術。
- 用途明列包含「預防與調查詐欺、濫用、違反 Usage Policy」「執行 ToS」。

### 7.2 Consumer Terms 的重點

- 必須提供**正確、最新、完整**的帳號資訊；不得共用帳密或讓他人使用帳號。
- 可隨時終止你的存取：**若認定你違反條款，可不經通知停權或終止，且訂閱不退費**。
- 非因違約而終止時，才按比例退還剩餘期間費用。
- 除條款明定或法律要求外，**付款一律不退**；巴西／墨西哥／韓國／台灣居民有 7 日內解約權（法定）。
- 不得逆向工程（除法律禁止此限制外）。

### 7.3 Usage Policy（AUP）— 直接命中「多帳號」

明文列為禁止行為：

- 「Coordinate malicious activity across multiple accounts, or **create multiple accounts**, to avoid
  detection, circumvent product guardrails…」
- 「**Circumvent a ban through the use of a different account**, such as the creation of a new account…」
- 「Intentionally bypass or interfere with… **circumvention of the account sign-up or log-in process**」
- 「Access… our products or services in violation of our Supported Regions Policy」
- 「Use stolen, fraudulent, or unauthorized payment methods」
- 「Resell, proxy, or otherwise provide access to Claude through unauthorized means… or misrepresent the
  product or client being used」

### 7.4 Supported Regions Policy

只允許清單內的國家／地區；並明確寫出三種也算違規：**人在未支援地區**（含支援實體的員工出差）、
實體註冊於未支援地區、由未支援地區的人多數持股或控制。清單不含中國大陸、香港、澳門。

### 7.5 封禁與申訴（官方 Help Center）

封禁理由明列三類：反覆違反 Usage Policy、**從未支援地點建立帳號**、違反 ToS。
被 ban 的帳號仍可登入 `claude.ai/restricted` 提出申訴、匯出資料、自我刪除。

### 7.6 資料用途與保存（Claude Code 官方文件）

- 消費者（Free/Pro/Max/Ultra）：可選擇是否用於訓練；**預設可開**。選「允許」保存 5 年，選「不允許」30 天。
- 商用（Team/Enterprise/API/第三方平台）：不訓練生成模型；標準保存 30 天；符合資格可申請 ZDR。
- `/feedback`、`/bug`、`/share` 的 transcript 保存 5 年；session 品質問卷的「同意上傳 transcript」是獨立第二步，
  上傳內容為原始 session log（僅遮罩已知 API key/token 樣式，程式碼原樣上傳）。

## 8. 所以「換個新帳號」會發生什麼

把上面的東西接起來：

1. 同一台機器、同一個 `~/.claude.json` → `device_id` 不變（第 1 節）。
2. 新帳號的 `account_uuid` 一出現，就與舊帳號的 `device_id` 在同一個 `metadata.user_id` 物件裡相遇。
3. 再加上同一組 repo 內容、同一組 commit message、同一組檔案路徑前綴（`/home/<name>/`）。
4. 伺服器端另有 IP、支付指紋、KYC 紀錄、註冊裝置指紋。

這不是「有沒有小動作」的問題，而是**架構上就留了 join key**。所以「秒封且無退款」不是意外，
是條款（7.2：違約終止不退費）與偵測（第 1 節）共同作用的結果。

也因為如此，我沒有把這份盤點寫成規避清單：一來 AUP 7.3 明文禁止多帳號與繞過封禁，
二來即使把 `device_id` 洗掉，第 6 節那些訊號仍然主導判斷——代價高、成功率低、且明確違約。

**比較值得走的路**（都不需要偽造身分）：

- **申訴**：`claude.ai/restricted`，用被封的帳號登入填表。KYC 未通過通常屬於可解釋的情況。
- **改用商用條款**：Console API、Amazon Bedrock、Google Cloud Agent Platform、Microsoft Foundry
  走 Commercial Terms，驗證主體是「組織／付款帳戶」，不是消費者 KYC；資料也不預設用於訓練。
  代價是按 token 計費、沒有 Pro/Max 的訂閱額度。
- **確認地區合規**：Supported Regions 清單是硬條件。若實際所在地不在清單內，任何技術手段都只是把
  違規點往後推。
- **法定權利**：若在 EU/UK 或巴西／墨西哥／韓國／台灣，退費與撤回權走當地消費者法，比工單有效。

---

## 附錄：證據片段

1. 攔截到的 `metadata`：
   `{"user_id":"{\"device_id\":\"<64hex>\",\"account_uuid\":\"\",\"session_id\":\"<uuid>\"}"}`
2. `~/.claude.json` 的 `userID` 與上述 `device_id` 數值相同。
3. 系統上下文模板：`Here is useful information about the environment you are running in:\nWorking directory: …\nIs directory a git repo: …\nPlatform: …\nOS Version: …`
4. `Git user: ${A}` 的 `A` 來自 `["config","user.name"]`。
5. `EGRESS_SERVICES={"vm-bundle":…,"updater":…,"sentry":{toggleKey:"disableEssentialTelemetry"…},"datadog-rum":…,"segment":{toggleKey:"disableNonessentialTelemetry"…},"anthropic-telemetry":…}`
6. `r4={organization_uuid:Rr,account_uuid:Rr}`（telemetry 事件 schema 同時校驗兩者）。
7. `ck={event_name,…,device_id:To(q2),user_type:nn("ant","external"),…}`
8. `dr=[{label:"$HISTFILE",…},{label:"~/.zsh_history",…},{label:"~/.bash_history",…},…]`
9. 憑證 deny-list：`\.netrc|\.gnupg\/|\/etc\/shadow|\.kube\/config|\.docker\/config\.json|\.npmrc|\.pypirc|\.git-credentials|…`
10. `OSr="https://http-intake.logs.us5.datadoghq.com/api/v2/logs",L4t="pubea5604404508cdd34afb69e6f42a05bc"`
