# codex-pool

以 ChatGPT 訂閱額度驅動的 Codex 後端，內建多帳號憑證池與輪替。

一組 `codex login` 產生的憑證匯入之後，這個套件負責：挑帳號（sticky，用到撞額度才換）、
撞到 429 時依回應中的精確 `resets_at` 冷卻、access token 過期時 refresh、
帳號真的死掉時退休它——全部在**一次呼叫內部**完成，呼叫端只看到一個成功或一個錯誤。

每個接縫都是小介面，任何服務都能嵌入。

## 安裝

```bash
uv add codex-pool          # 或 pip install -e path/to/codex-pool
```

## 取得憑證（bootstrap）

每個 ChatGPT 帳號一個獨立目錄，各登入一次：

```bash
CODEX_HOME=~/.codex-pool-accounts/acct-a codex login
CODEX_HOME=~/.codex-pool-accounts/acct-b codex login

codex-pool-import --store ./codex-accounts.json ~/.codex-pool-accounts/acct-a ~/.codex-pool-accounts/acct-b
```

匯入之後這些目錄就沒用了，可以刪除；**部署環境不需要安裝 codex CLI**。

> ⚠️ 不要匯入 codex CLI 日常在用的 `CODEX_HOME`（例如預設的 `~/.codex`）。
> 後端每次 refresh 都會輪替 refresh token，池一旦動手，CLI 手上那份就失效了。
> 一個帳號給一個專用目錄。

## 使用

```python
from codex_pool import (
    ChatRequest, CodexPool, CodexPoolConfig, CodexTransport,
    CompositeProvider, JsonFileStore, Message, Role, resolve_tier_models,
)

config = CodexPoolConfig()
pool = CodexPool(JsonFileStore("codex-accounts.json"), config=config)
provider = CompositeProvider(
    pool=pool,
    codex=CodexTransport(config=config),
    resolver=lambda cred: resolve_tier_models(cred, config=config),
)

response = await provider.chat(ChatRequest(
    model="balanced",                    # tier 名稱，或直接給 "gpt-5.6" 這種 slug
    messages=[Message(role=Role.USER, content="嗨")],
))
print(response.content)
```

串流：

```python
async for chunk in await provider.stream(request):
    print(chunk.delta, end="")
```

結構化輸出（等同 Codex CLI 的 `--output-schema`）：

```python
import json
from codex_pool import drop_nulls, strict_schema

request = ChatRequest(
    model="balanced",
    messages=[...],
    output_schema=strict_schema(MyModel.model_json_schema()),
)
content = (await provider.chat(request)).content
payload = MyModel.model_validate(drop_nulls(json.loads(content)))
```

兩個 helper 要成對使用：

- `strict_schema()` 把 pydantic schema 轉成 strict 模式要的形狀
  （每個物件 `additionalProperties: false`、所有欄位進 `required`）。
- `drop_nulls()` 把回來的 payload 裡值為 `null` 的鍵拿掉。**這步不能省**：
  因為所有欄位都被列為必填，模型會對沒話說的欄位回 `null`，而 `null`
  並不滿足 `note: str = ""` 這種非 nullable 標註——pydantic 會直接報錯，
  不會套用預設值。拿掉鍵才等於「沒給」，預設值才會生效。

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
| **`client_version` 過舊會警告** | 後端是「靜默隱藏」新模型而不是報錯，不講的話只會表現為 tier 悄悄降了一代 |

## Store

`CodexPool` 只依賴一個介面：`transaction()`——一個 async context manager，
交出 `account_id -> AccountRecord` 的可變映射，正常離開就原子提交，拋例外就丟棄。
池的正確性（同時只有一個 active、refresh 不會兩個人同時做）建立在「transaction 之間互斥」上。

內建的 `JsonFileStore` 用 `asyncio.Lock` 加 JSON 檔實作，**單一行程內**成立。
多 worker 部署要自備 DB 版的 store（實作同一個介面即可：
`SELECT ... FOR UPDATE` 搭配 `active` 的部分唯一索引，就是跨行程的同一件事）。

Store 檔案裡是明文的 bearer 與 refresh token，比照私鑰保管：POSIX 上會設成
owner-only；Windows 上靠所在目錄的 ACL，跟 `~/.codex/auth.json` 本身一樣。

## 開發

```bash
uv sync
uv run pytest -q
```

## 授權與定位

這個套件模擬 Codex CLI 的身分（`originator: codex_cli_rs`）呼叫 ChatGPT 訂閱端點，
屬於**非官方 API 用法**，端點與 wire 格式都可能無預警變動。適合自用、開發、
內部驗收；要拿來對外營運服務前，請先確認符合 OpenAI 當下的使用條款，
並認真評估改用正規計價 API。
