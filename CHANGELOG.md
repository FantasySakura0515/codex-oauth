# 變更紀錄

版本號依 [語意化版本](https://semver.org/lang/zh-TW/)。

## 0.3.0

套件改名：**codex-pool → tidepool**。額度窗口像潮汐一樣定期回充，帳號冷卻後
自己回到池裡——名字跟著機制走，也不再掛著後端的商標。0.2.0 以前的紀錄仍以
舊名稱呼各物件。

### 破壞性變更

- 發佈名 `codex-pool` → `tidepool`，import 名 `codex_pool` → `tidepool`。
- `CodexPoolClient` → `TidepoolClient`，`CodexPoolConfig` → `TidepoolConfig`。
  `provider_name` 隨之改為 `"tidepool"`。
- 憑證匯入 CLI `codex-pool-import` → `tidepool-import`。
- 環境變數前綴 `CODEX_POOL_*` → `TIDEPOOL_*`。**過渡期相容**：`from_env()`
  仍會回讀舊前綴，但寫進 log 提醒改名；下一個 minor 版本移除。
- Codex 後端專屬的類別（`CodexPool`、`CodexTransport`、`CodexCredential` 等）
  維持原名——它們描述的是後端，不是套件。

### 不變

- 憑證庫檔案格式與既有路徑照舊；`~/.codex-pool-accounts` 這類舊目錄不需搬家，
  路徑本來就是明示傳入的。

## 0.2.0

新增 `CodexPoolClient`——一行接上的 LLM 介面。在此之前呼叫端得自己組
`CodexPool` + `JsonFileStore` + `CodexTransport` + `CompositeProvider` + resolver
五個物件，還得自己知道 `strict_schema()` 與 `drop_nulls()` 必須成對、async 要
自己橋到同步。那些現在都在套件裡。

### 新增

- `CodexPoolClient`：`complete` / `complete_structured` / `stream` / `status`，
  以及 `a` 開頭的 async 雙生方法。`CodexPoolClient.from_env()` 讀 `CODEX_POOL_*`。
- `LLMClient` 協定與 `LLMUnavailableError`，供應用程式標型別、接錯誤。
- 結構化輸出只需給 pydantic model；strict schema 收緊與回傳的 null 剔除都在內部。
- 模型清單每天自動重查一次（`CodexPoolConfig.catalogue_ttl_seconds`，
  或 `CODEX_POOL_CATALOGUE_TTL`）。換代時發出 `tier models changed` 警告。
- `py.typed`：下游的型別檢查器現在看得到本套件的標註。
- GitHub Actions CI（Linux 3.11／3.12／3.13 加 Windows 3.12）與 ruff 設定。
- MIT 授權。

### 修正

- assistant 歷史訊息改用 `output_text`。Responses API 對 input item 的
  content 型別分得很死，混用 `input_text` 會 400。
- tier 對應跳過非聊天用的模型，`fast` 改以 `-mini`／`-nano` 命名優先挑選。
  線上 catalogue 尾端是 `codex-auto-review`（程式碼審查功能專用），
  照「最後一個＝最便宜」挑會把一般提問送去那裡。
- 更新 pinned 的墊底 id 為 `gpt-5.4-mini`／`gpt-5.6-terra`／`gpt-5.6-sol`。
  原本的 `gpt-5.6` 與 `gpt-5.6-mini` 已從線上清單下架。
- catalogue 重查失敗時保留上一次的清單，不退回 pinned。
- 同步方法在事件迴圈裡改為明確拋錯，而非默默阻塞呼叫端的迴圈。

### 行為變更

- `CompositeProvider` 新增 `catalogue_ttl_seconds` 參數。預設每天重查一次；
  設 `0` 維持 0.1.0 的行為（一個 instance 只解析一次）。

## 0.1.0

初版：以 ChatGPT 訂閱額度呼叫 Codex Responses 端點，內建多帳號憑證池。
sticky 選帳號、429 依 `resets_at` 精確冷卻、token refresh 與 permanent／
transient 失敗分類、模型清單動態解析、`codex-pool-import` 憑證匯入 CLI。
