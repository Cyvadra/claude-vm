# Claude Code CLI 身分識別欄位靜態分析

對象：`~/.local/share/claude/versions/2.1.316`（Bun standalone，314 MB）
手法：`strings` + `grep -a -o -E` 取二進位內嵌 JS 片段；`~/.claude.json` 只讀 key 與型別。
結論標記：**[直證]** = 二進位字串直接證據；**[推論]** = 由片段結構推得。

## 一、machineID 怎麼來的

- **隨機產生後持久化，不由系統特徵推導。** [直證]
- `getOrCreateMachineID()` 先讀全域 config 的 `machineID`，若符合 `/^[0-9a-f]{64}$/` 即沿用；否則 `crypto.randomBytes(32).toString("hex")` 產生 64 字元 hex，再寫回 `~/.claude.json`。
- 全 binary 只有一處寫入 `machineID`（`setGeneratedMachineID`），無任何讀取 hostname／MAC／CPU／`/etc/machine-id` 參與此值的路徑。
- **易混淆點**：binary 內確實有讀 `/etc/machine-id`、`/var/lib/dbus/machine-id` 的 `getMachineId()`，但那是 OpenTelemetry `hostDetector` 用來填 `host.id` resource attribute，與 config 的 `machineID` 是兩個獨立欄位。 [直證]

## 二、userID 綁機器還是綁帳號

- **綁機器：首次執行時本機隨機產生，非伺服器下發。** [直證]
- `getOrCreateUserID()` 結構與 machineID 完全相同：config 有 64-hex 就沿用，否則 `crypto.randomBytes(32).toString("hex")` 寫回 config。
- 全 binary 對 `userID` 只有兩處賦值（`setGeneratedUserID` / 對應 `Pe` 寫入），**不存在** OAuth 登入後以伺服器值覆寫的路徑。 [直證]
- 用途：telemetry row 的 `device_id`、事件的 `user_id`、OTEL resource `user.id`、實驗分桶 `sha256(userID)` 取整數。
- 現況驗證：`~/.claude.json` 同時存在 `machineID`、`userID`，兩者皆為 64 字元 hex。

## 三、請求帶的識別 header / metadata

- **User-Agent**：`claude-code/2.1.316`，可附加 `, client-app/<id>`、`, workload/<id>`。 [直證]
- **`x-app`**：`cli`，背景程序為 `cli-bg`。 [直證]
- **`X-Claude-Code-Session-Id`**：每 session 一個，隨 header 送出。 [直證]
- **`anthropic-client-platform`**：值由 `o_()` 提供，值域含 `cli`、`desktop_app`；另有 `anthropic-client-version`（desktop 情境）。 [直證]
- **`x-organization-uuid`**、**`X-Trusted-Device-Token`**：org／裝置信任情境。 [直證]
- **`anthropic-version: 2023-06-01`**、**`anthropic-beta: <beta 清單>`**：協議用，非識別。 [直證]
- **`x-stainless-*`**：靜態字串只命中 `x-stainless-helper`、`x-stainless-helper-method`（且出現在「可被覆寫／剝除的 header 名」白名單邏輯中）。`x-stainless-lang/os/arch/package-version` 等由 SDK runtime 組裝，字串表未命中。 [直證 + 推論]
- **telemetry 端點**：
  - `https://api.anthropic.com/api/event_logging/v2/batch`（第一方事件批次，batch ≤200） [直證]
  - `browser-intake-datadoghq.com` 及其 EU/AP/US3 變體（Datadog RUM/日誌） [直證]
  - `o1158394.ingest.us.sentry.io`（Sentry，`DISABLE_ERROR_REPORTING` 可關） [直證]
  - statsig：只命中 `cachedStatsigGates`、`cachedGrowthBookFeatures` 等快取 key 與 `~/.claude/statsig` 目錄名，**端點 URL 未命中**。 [部分]
- **event_logging payload 欄位**：`event_id`、`device_id`（= userID）、`session_id`、`email`、`agent_id`、`parent_session_id`、`agent_type`、`server_timestamp`、`auth`、`data`、`swe_bench_run_id/instance_id/task_id`。 [直證]

## 四、識別欄位總表

| 欄位 | 存放位置 | 產生方式 | 換機是否變 | 換帳號是否變 |
|---|---|---|---|---|
| `machineID` | `~/.claude.json`（全域） | 本機 `randomBytes(32)` → 64 hex，首次寫入後固定 | 變（新檔新值） | 不變 |
| `userID` | `~/.claude.json`（全域） | 同上，獨立隨機 | 變 | 不變 |
| `remoteControlMachineId` | `~/.claude.json` | `crypto.randomUUID()`（UUIDv4 格式校驗） | 變 | 不變 |
| `summonSidKey` | `~/.claude.json` | 同上 64 hex | 變 | 不變 |
| `host.id`（OTEL） | 不落盤，執行時算 | 讀 `/etc/machine-id` 或 `/var/lib/dbus/machine-id` | 變 | 不變 |
| `session_id` | 記憶體／session 檔 | 每 session 隨機 | 每次變 | 每次變 |
| `accountUuid` / `organizationUuid` | 憑證檔（非 `.claude.json`） | 伺服器下發（登入後） | 不變 | 變 |
| `email` | 憑證／telemetry row | 伺服器下發 | 不變 | 變 |

## 五、證據附錄（每條 ≤200 字元）

1. `typeof r.machineID==="string"&&ma.test(r.machineID))return r.machineID;...let s=mc(32).toString("hex");return n.setGeneratedMachineID(s),Pe((h)=>({...h,machineID:s}),e),s`
2. `typeof s.userID==="string"&&ma.test(s.userID))return s.userID;...let h=n!==void 0&&ma.test(n)?n:mc(32).toString("hex");return r.setGeneratedUserID(h),Pe((y)=>({...y,userID:h}),e),h`
3. `var ma=/^[0-9a-f]{64}$/;`
4. `import{randomBytes as mc,randomUUID as II}from"crypto"`
5. `c=["/etc/machine-id","/var/lib/dbus/machine-id"];for(let a of c)try{return(await t.promises.readFile(a,{encoding:"utf8"})).trim()`
6. `[pt.ATTR_HOST_ID]:My.getMachineId()`（OTEL hostDetector）
7. `function Xs(){return` + UA 模板 → `claude-code/2.1.316`；另見附加段 `, client-app/${i.CLAUDE_AGENT_SDK_CLIENT_APP}`、`, workload/${h}`
8. `{"x-app":Ft()?"cli-bg":"cli","User-Agent":wU(),[n8t]:K()}`；`n8t="X-Claude-Code-Session-Id"`
9. `"anthropic-client-platform":o_()`；`"anthropic-version":"2023-06-01"`
10. `i["x-organization-uuid"]=r`；`"X-Trusted-Device-Token"`
11. `"https://api.anthropic.com/api/event_logging/v2/batch"`、`this.maxBatchSize=e.maxBatchSize||200`
12. `device_id:ke(e.device_id)?globalThis.String(e.device_id):"",swe_bench_run_id:...`
13. `s["user.id"]=e`（e 來自 `XH()`）
14. `let e=XH(),n=M("sha256").update(e).digest("hex");return parseInt(n.sli...`（實驗分桶）
15. `remoteControlMachineId` 校驗用 `uD=/^[0-9a-f]{8}-[0-9a-f]{4}-...-[0-9a-f]{12}$/`

## 六、未解問題

1. **statsig 端點與 payload 未命中**：binary 只留快取 key（`cachedStatsigGates`），未見 `statsigapi.net` / `featuregates.org` 之類字串，可能經由封裝層或第一方代理轉發，需 runtime 抓包確認。
2. **`x-stainless-*` 實際送出集合**：靜態字串不足，需實際攔一次請求才知 SDK 注入哪些。
3. **`cachedExperimentData` / `cachedGrowthBookFeatures` 的請求格式**未展開（含 bucket 值、gate 清單）。
4. **Sentry 是否預設啟用**：binary 有 DSN 字串，但觸發條件（`DISABLE_ERROR_REPORTING`、`DO_NOT_TRACK`、`NONESSENTIAL_TRAFFIC`）的組合未逐一驗證。
5. `machineID` 在一般 `/v1/messages` 請求中是否出現：目前只在 remote-control bridge 註冊（`machineId:Dt`）與 telemetry 觀察到，未見於 messages 請求 header。
