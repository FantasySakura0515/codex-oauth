# codex-oauth

[![CI](https://github.com/FantasySakura0515/codex-oauth/actions/workflows/ci.yml/badge.svg)](https://github.com/FantasySakura0515/codex-oauth/actions/workflows/ci.yml)

用 ChatGPT **訂閱額度**（非 API 金鑰）驅動的 LLM client。把多個帳號的憑證匯進一個池，
指向它、呼叫、結束——帳號輪替、429 冷卻、token refresh、模型解析、strict 模式結構化輸出
全部在一次呼叫內部完成，呼叫端只會看到一個成功、一個 `ValidationError`，
或一個 `LLMUnavailableError`。

名字直說取用方式：以 `codex login` 留下的 **OAuth 憑證**呼叫 Codex 端點，
額度算在 ChatGPT 訂閱上，全程沒有 API 金鑰。
（0.3.0 前叫 `codex-pool`；遷移事項見 [CHANGELOG.md](CHANGELOG.md)。）

## 架構

一個門面、四層機制，全部裝在單一 Python 套件裡：

| 層 | 模組 | 說明 |
|---|---|---|
| **門面** | `client.py` | `CodexOAuthClient`：同步/async 的 `complete`／`complete_structured`／`stream`，對外唯一需要認識的東西 |
| 組合 | `composite.py` | `CompositeProvider`：把池、模型解析與可選的 fallback provider 串成一條呼叫鏈 |
| 帳號池 | `pool.py`、`store.py` | `CodexPool`：sticky 選帳號、429 冷卻、refresh 輪替、退休判定；憑證放 `JsonFileStore`（可換成自己的 `AccountStore`） |
| 後端通訊 | `transport.py`、`wire.py` | Codex Responses 端點的 HTTP／SSE 與 wire 格式、strict schema 處理 |
| 模型清單 | `catalogue.py` | `GET /models` 動態解析 tier→模型，每天重查一次 |

呼叫路徑：`CodexOAuthClient → CompositeProvider → CodexPool（AccountStore）→ CodexTransport`。
`Codex*` 開頭的類別描述的是後端（Codex Responses 端點），不隨套件改名。

## 快速開始

前置：Python 3.11+、[codex CLI](https://github.com/openai/codex)（只在匯入憑證那一步需要）。

```bash
uv add codex-oauth          # 或 pip install -e path/to/codex-oauth
```

### 匯入憑證（一次性）

每個 ChatGPT 帳號一個獨立目錄，各登入一次，然後匯成一個憑證庫檔：

```bash
CODEX_HOME=~/.codex-oauth-accounts/acct-a codex login
CODEX_HOME=~/.codex-oauth-accounts/acct-b codex login

codex-oauth-import --store ./accounts.json \
    ~/.codex-oauth-accounts/acct-a ~/.codex-oauth-accounts/acct-b
```

匯入之後這些目錄就沒用了，可以刪除；**部署環境不需要安裝 codex CLI**。
多加帳號就多跑一次這兩步，池會自己接手。

> ⚠️ 不要匯入 codex CLI 日常在用的 `CODEX_HOME`（例如預設的 `~/.codex`）。
> 後端每次 refresh 都會輪替 refresh token，池一旦動手，CLI 手上那份就失效了。
> 一個帳號給一個專用目錄。

憑證庫檔案裡是明文 bearer 與 refresh token，比照私鑰保管，**不要進版控**。

### 呼叫

```python
from codex_oauth import CodexOAuthClient

llm = CodexOAuthClient("accounts.json")   # 或 CodexOAuthClient.from_env()

llm.complete("一句話介紹光合作用")
```

環境變數版（`from_env()`；舊的 `CODEX_POOL_*` 前綴過渡期仍會回讀，但 log 會提醒改名）：

| 變數 | 說明 |
|---|---|
| `CODEX_OAUTH_STORE` | **必填**，憑證庫路徑 |
| `CODEX_OAUTH_MODEL` | tier 名稱（`fast`／`balanced`／`strong`）或具體 slug，預設 `balanced` |
| `CODEX_OAUTH_CLIENT_VERSION` | 需與產生憑證的 codex CLI 一致 |
| `CODEX_OAUTH_TIMEOUT` | 每次呼叫的秒數上限，預設 300 |
| `CODEX_OAUTH_CATALOGUE_TTL` | 多久重查一次模型清單，預設 86400（一天）；設 0 表示只查一次 |

## 使用

### 結構化輸出

給一個 pydantic model，拿回一個驗證過的物件。schema 由**伺服器強制**
（Responses strict 模式，等同 Codex CLI 的 `--output-schema`），
不是把 schema 塞進 prompt 用拜託的：

```python
from pydantic import BaseModel

class Report(BaseModel):
    title: str
    notes: str = ""

report = llm.complete_structured(Report, "幫我寫一份摘要")
```

也接受全具名的呼叫形式，方便直接套進既有的 client 協定：

```python
report = llm.complete_structured(
    system="你是健康教育老師", messages=[...], response_model=Report
)
```

### 串流

```python
for delta in llm.stream("寫一首短詩"):
    print(delta, end="")
```

### async

方法預設是同步的（多數應用程式碼是同步的），底下橋到一條常駐事件迴圈執行緒。
在事件迴圈裡改用 `a` 開頭的雙生方法——同步版會直接拒絕執行，
而不是默默把你的迴圈卡住：

```python
await llm.acomplete("...")
await llm.acomplete_structured(Report, "...")
async for delta in llm.astream("..."):
    ...
```

### 錯誤

只有兩種，而且分得很開：

- **`LLMUnavailableError`**——拿不到答案：池乾了、憑證全退休、逾時、後端故障。
  應用程式接這個就夠了。
- **`ValidationError`**（pydantic 的）——拿到答案但不合 model。
  這是「換個 prompt 再試一次」的情況，不是後端壞了，所以刻意不併成同一個。

用協定標型別，應用程式就不綁在這個後端上：

```python
from codex_oauth import LLMClient, LLMUnavailableError

def summarise(llm: LLMClient) -> Report:
    return llm.complete_structured(Report, "...")
```

### 池的健康狀況

```python
for account in llm.status():
    print(account.label, account.status, account.cooldown_until)
```

## 設計要點

| 行為 | 為什麼 |
|---|---|
| **Sticky 選帳號**（一個用到底，不平均分攤） | 額度是按帳號算的；分散只會讓每個帳號都半殘 |
| **429 依 `resets_at` 冷卻**，無值時才用預設 | 後端不公布額度上限，但 429 內容帶精確重置時間——猜不如讀 |
| **冷卻時間夾在 min/max 之間**；但已過期的 `resets_at` 直接放行 | 前者防止「2 秒後重試」的熱迴圈，後者代表額度窗口已經翻轉，再罰就是白白扣住可用帳號 |
| **refresh 失敗分 permanent／transient** | `disabled` 是終局狀態，只有重新匯入能救。把一次 5xx 或斷線當成永久失效，會讓 auth server 抽風的一分鐘掃掉整池 |
| **未分類的例外一律不退休帳號** | 同上，寧可下次再試 |
| **串流已吐出內容後不輪替** | 重跑會讓使用者看到答案從頭再來一次；改為拋出錯誤，該帳號仍會被冷卻 |
| **`CodexTransportError` 不觸發輪替** | 它代表後端打嗝，不代表這個帳號有問題；輪替只會多付一次錢又退休健康的帳號 |
| **200 但沒有終結事件＝失敗** | 靜默截斷跟「答案本來就短」無法區分，半截的結構化輸出下游會解析成垃圾 |
| **模型清單走 `GET /models` 動態解析**，pinned id 只是墊底 | 後端每幾週換一代並淘汰舊 slug，寫死的 id 遲早讓每個請求都 400 |
| **每天自動重查一次模型清單**（`catalogue_ttl_seconds`） | 只查一次的話，長跑不重啟的服務會永遠停在啟動當下那一代；換代時 log 會有 `tier models changed` 警告 |
| **重查失敗時保留上一次的清單**，不退回 pinned | 上一次的答案來自真實 catalogue，比寫死的 id 新；且失敗一樣記時間戳，壞掉的 catalogue 會等下一個 TTL 再試，不會每個請求都多付一次白工 |
| **tier 對應會跳過非聊天用的模型**，`fast` 優先挑 `-mini`／`-nano` | catalogue 裡混著單一功能專用的項目（例如 `codex-auto-review`），它們在 priority 上排在最便宜那端；照「清單最後一個就是最便宜」去挑，會把一般提問悄悄送去程式碼審查專用的模型 |
| **`client_version` 過舊會警告** | 後端是「靜默隱藏」新模型而不是報錯，不講的話只會表現為 tier 悄悄降了一代 |
| **strict schema 與 drop-null 成對處理** | strict 模式把所有欄位列為必填，模型會對沒話說的欄位回 `null`，而 `null` 不滿足 `notes: str = ""` 這種非 nullable 標註；拿掉鍵才等於「沒給」，預設值才會生效 |

## 底層 API

`CodexOAuthClient` 是把底下各層組起來的門面。要自己組（換掉 store、接 fallback
provider、或在既有的 async 服務裡直接驅動）時，元件都還在：

```python
from codex_oauth import (
    ChatRequest, CodexPool, CodexTransport, CompositeProvider,
    JsonFileStore, Message, Role, CodexOAuthConfig, resolve_tier_models,
)
```

`CodexPool` 只依賴一個介面：`AccountStore.transaction()`——一個 async context
manager，交出 `account_id -> AccountRecord` 的可變映射，正常離開就原子提交、
拋例外就丟棄。池的正確性（同時只有一個 active、refresh 不會兩個人同時做）
建立在「transaction 之間互斥」上。

內建的 `JsonFileStore` 用 `asyncio.Lock` 加 JSON 檔實作，**單一行程內**成立。
多 worker 部署要自備 DB 版的 store（實作同一個介面即可：
`SELECT ... FOR UPDATE` 搭配 `active` 的部分唯一索引，就是跨行程的同一件事）。

## 疑難排解

| 現象 | 原因與處置 |
|---|---|
| `codex-oauth store not found` | 還沒匯入憑證，或 `CODEX_OAUTH_STORE` 路徑錯 |
| `every codex account is cooling or disabled` | 全部帳號都在冷卻或已退休。看 `llm.status()` 的 `status` 與憑證庫的 `last_error`；若是 `refresh failed ... HTTP 401`，重跑 `codex login` 後**重新匯入**即可救回（匯入會清掉退休狀態） |
| 冷卻時間看起來很長 | 那是後端回報的真實 `resets_at`；訂閱制的額度窗口實測可達數天 |
| 模型 id 不是預期的那個 | tier 由 `GET /models` 動態解析；`CODEX_OAUTH_CLIENT_VERSION` 太舊時後端會**靜默隱藏**新世代模型，log 會有警告 |
| 同步方法在事件迴圈裡拋 `RuntimeError` | 那是刻意的，改用 `acomplete` / `acomplete_structured` / `astream` |
| log 出現 `CODEX_POOL_* is the pre-rename spelling` | 環境變數還是 0.2.x 的舊前綴，改成 `CODEX_OAUTH_*` 即可；過渡期功能不受影響 |

## 測試與開發

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
```

CI（`.github/workflows/ci.yml`）跑 Linux 的 3.11／3.12／3.13 加 Windows 3.12。
Windows 有自己的一格是因為這裡最花時間的兩個 bug——預設編碼與 CRLF 行尾——
在 Linux 上都不會重現。

版本變更見 [CHANGELOG.md](CHANGELOG.md)。

## 專案結構

```
src/codex_oauth/
  client.py       CodexOAuthClient 門面（同步/async、from_env）
  composite.py    CompositeProvider：池 + 模型解析 + fallback 的呼叫鏈
  pool.py         CodexPool：帳號輪替、冷卻、refresh、退休
  store.py        AccountStore 介面與 JsonFileStore
  transport.py    Codex Responses 端點的 HTTP／SSE
  catalogue.py    模型清單動態解析（tier → slug）
  wire.py         wire 格式、strict schema 與 drop-null
  import_cli.py   codex-oauth-import 憑證匯入 CLI
  config.py       CodexOAuthConfig
tests/            單元與整合測試（respx 假後端）
```

## 授權與定位

程式碼以 [MIT](LICENSE) 授權。

這個套件模擬 Codex CLI 的身分（`originator: codex_cli_rs`）呼叫 ChatGPT 訂閱端點，
屬於**非官方 API 用法**，端點與 wire 格式都可能無預警變動。適合自用、開發、
內部驗收；要拿來對外營運服務前，請先確認符合 OpenAI 當下的使用條款，
並認真評估改用正規計價 API。
