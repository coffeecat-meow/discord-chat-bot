# 模型路由與備援

4.6 的模型、服務、金鑰名稱都由 TOML 配置。聊天大模型（chat）、輕量模型（light）、視覺模型（vision）各有獨立 primary／fallback；範本目前選 Go 優先、OpenRouter 備援，程式不固定此順序。主備可以使用不同模型和不同金鑰。

| 配置群組 | 用途 | 未覆寫時 |
|---|---|---|
| chat | 普通聊天與工具接續 | 必填主路由 |
| light | 普通接話分類、背景摘要／畫像 | 各欄繼承 chat 同一主／備槽 |
| vision | 圖片理解 | 各欄繼承 chat 同一主／備槽 |
| attention（可選） | 單獨覆寫普通接話分類 | 沿用 light；存在獨立群組時按欄繼承 chat |
| memory（可選） | 單獨覆寫背景模型 | 沿用 light；存在獨立群組時按欄繼承 chat |
| task（可選） | 專用任務指令 | 按欄繼承 chat |

下方`:floor`僅示範可自行指定的模型ID；部署範本的OR備援為未加後綴的同模型，本目標沒有實際呼叫OR。各群組完整填寫最清楚；部署範本已為 chat／light／vision 各寫一對，見 [runtime.example.toml](../config/runtime.example.toml)。例如：

```toml
[model.routes.vision.primary]
base_url = "https://opencode.ai/zen/go/v1"
model = "deepseek-v4.1-flash"
api_key_env = "OPENCODE_API_KEY"
api_format = "opencode"

[model.routes.vision.fallback]
enabled = true
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4.1-flash:floor"
api_key_env = "OPENROUTER_API_KEY"
api_format = "openrouter"
```

`api_key_env` 是環境變數名稱，TOML 不填金鑰本體。現有 shell 環境優先於配置 root 的 `.env`。可將大小視覺模型指向不同服務、使用不同變數；明確要求 `OPENCODE_API_KEY` 時不會挪用舊 `LLM_API_KEY`。僅為既有檔相容接受 Go 小寫名稱 `opencode_api_key`。`api_format` 可選 opencode／openrouter／openai／auto；auto 依 hostname 判斷，其他相容服務採 openai。非已驗模型須自行驗其影像與推理能力；可填相容endpoint不代表所有服務都接受相同推理欄位，這次完整轉譯驗證限Go DS V4.1／OR。

新 routes 配置優先於舊 `model.base_url/chat/attention/memory/vision/task`。完全沒有 routes 的舊配置維持單 endpoint 且沒有自動備援；請勿混用兩種寫法。主路由不可停用；備援可設 enabled=false。沒有備援 key 時記錄不可用，不能偷換主 key。

## 切換與期限

每次生成先嘗試該用途 primary。SDK/API、連線及逾時錯誤才走一次 fallback；取消、本機參數錯誤、空正文或輸出截斷不觸發服務切換，沿用 Engine 既有處理。切換只重送當次模型請求，不重做已完成工具或已送前言。整次生成共用單一期限，有備援可用時主路由最多使用60%、備援使用剩餘時間；設定8秒的分類仍共用8秒。routes 模式 SDK max_retries=0，避免兩端各自隱藏多次重試。

`usage` 逐次保存用途、主備與非機密路由資訊；`model_failover` trace 和 warning log 記主／備 host、用途與錯誤類別，不記 key、完整 prompt 或供應商錯誤正文。備援也失敗就交回既有回合失敗路徑。

## 供應商參數

Go 每回合以客戶端自有 User-Agent 與穩定 UUID x-opencode-session 傳送。Go DS V4.1 的分類關推理使用原生 thinking.type=disabled，背景 low 使用原生 reasoning_effort；OpenRouter 備援另用自己的 reasoning 欄位。普通聊天 effort 留白仍為供應商預設；已選的最後工具 low 只套指定接續。參數不能跨服務照抄。

服務介面依 [OpenCode Go 官方文件](https://opencode.ai/v2/docs/console/go/) 與 [DeepSeek thinking 文件](https://api-docs.deepseek.com/guides/thinking_mode/) 核對；目前範本舊 Go URL 已實測，官方新文件列 inference/go/openai/v1，不自動遷移使用者配置。

## 本次驗證界線

RC 目標禁止實際 OpenRouter 出站，備援以真實 SDK＋httpx.MockTransport 驗主成功／主失敗／雙失敗、獨立模型／key、供應商順序交換、取消與期限。Go 有實際合成用途觀測；各批來源、呼叫帳本及限制以 [RC 主交付](handoff/RC-COMPLETION.md) 為準。模型回傳正文不等於整個用途或真 Discord 已驗收。
