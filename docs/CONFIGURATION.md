# 設定與優先序

大小視覺模型、各自模型名稱與主／備服務已配置化；請先讀 [模型路由](MODEL_ROUTES.md)。下方 `model.chat` 等名稱僅適用沒有 routes 的舊單服務配置。

聊天／任務的初次生成與一次空正文重試共用 `model.timeout_seconds`（預設90秒），不是每次重試重設期限；空正文額度整回合一次，與SDK `max_retries`或工具接續上限不同。任務另保留原整體期限。正常回覆不增加請求，記憶／視覺／參與分類不套這項重試。詳見[空正文交付](handoff/EMPTY-REPLY-AND-MEMORY-GUIDANCE-DELIVERY.md)。
`--config` 優先於 `SAYUKI_CONFIG`，未指定時讀專案 `config/runtime.toml`。TOML 放在 `config/` 時相對路徑基於該資料夾的上層，其他位置基於配置所在目錄；可設頂層 `project_root`（相對配置所在目錄）覆寫。不依目前 shell 目錄或安裝套件位置決定資料庫位置。wheel安裝請明確指定配置。

`.env` 放 BOT_TOKEN、路由指定的金鑰變數（範本為 OPENCODE_API_KEY／OPENROUTER_API_KEY）及可選 SAYUKI_CONFIG；舊單一路由仍接受 LLM_API_KEY。現有進程環境優先於 `.env`。明確指定配置時讀配置root內的 `.env`；未指定時保留來源專案 `.env` 的相容流程。不自動沿用來源目錄的 `.env` 或 API key。TOML 依 [runtime.example.toml](../config/runtime.example.toml) 建立。

| 區塊 | 作用 |
|---|---|
| discord | 管理員、信任伺服器、名字別名 |
| paths | 私人角色卡與新資料庫路徑 |
| storage | 診斷保存天數（30）；不設定原文／長期記憶 TTL |
| model | 主模型、記憶／視覺模型、推理、上限、timeout、retry |
| sleep | 統一作息、時區和睡眠狀態，預設00:30–08:00 |
| conversation | 原文窗口、佇列、工具輪數、複讀及主動參與 |
| delivery | 保留4.0分段人工等待的參數 |
| tools | 壓縮工具教學、結果大小與Obscura位置 |
| memory | 畫像預算、短期壽命、背景批次／間隔／每日呼叫上限、共享頻道 |

`model.chat` 不提供猜測模型預設。memory、vision 留空繼承 chat；使用圖片時需實際支援影像的模型。reasoning_effort 留空表示聊天採供應商預設，不額外插入推理參數。背景使用獨立的 `memory_reasoning_effort`，預設 low；設空字串可恢復背景供應商預設。不支援推理控制的服務需設空字串。

`memory.shared_channel_ids` 是明確允許互相共享的單一集合；來源和目的都要在其中且當下可讀。尚未提供多個共享群組或自動公開頻道共享。私訊不進入群聊事件儲存。

role/rules 不改寫。工具與記憶操作教學在送出時組裝，不修改來源檔案。角色卡作息若與配置不一致，需在部署前確認必要文字同步，勿以改配置暗中重寫角色卡。

`model.memory_max_output_tokens` 僅用於背景摘要／畫像 JSON，RC預設 8192；推理可能佔用供應商的輸出額度。它不改變 `model.max_output_tokens` 的聊天設定。背景排程仍受批次、冷卻、每日呼叫及重試上限約束。

2026-09-13 實測曾出現背景的 4096 輸出 token 全部用於推理、正文為空；因此背景改用 low，聊天 RP 推理維持供應商預設。這是當時的分工設定。2026-10-17依使用者確認，只把背景額度提高至8192並維持low；普通聊天不改，不新增模型階段。推理努力程度是供應商控制，不能保證永不截斷；截斷不寫入部分結果，仍使用既有有界重試。參數語意見 [OpenRouter reasoning 文件](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。

## 對話來源與接續額度（O5-C2）

`conversation` 新增四項；舊配置未填時採下表初值，不需修改角色卡。

| 名稱 | 初值 | 計量與用途 |
|---|---:|---|
| reply_source_chars | 1000 | 直接 reply 來源整筆 JSON 的字元上限，另於 history_chars 計算。若來源已完整在 recent_history，只放指標。 |
| reply_fetch_timeout_seconds | 2.0 | 快取沒有直接來源時的單次 fetch 期限；不遞迴 reply、不自動跨頻道。 |
| tool_merge_chars | 1500 | 每回合全部接續輪的新增訊息記錄 JSON 累計字元。引用包裝／省略計數為另計固定開銷。 |
| tool_merge_messages | 5 | 每回合全部接續輪的新增訊息累計則數。超額揭露省略量，不自動丟掉原排隊回合。 |

字元額度包括每筆資料的身分、時間、正文與附件欄位，並非 token 預估。超長來源／補充先去除附件及圖像觀測再縮短正文，帶 truncated。極小 reply 額度放不下身分時，只提供固定 budget_omitted 狀態。普通近期訊息不逐則縮短，受原總量與則數限制；被省略會記數。

Obscura 只由 `tools.obscura_bin` 設定；含目錄的相對路徑基於部署 root，裸名稱走 PATH。4.6 不讀舊 `OBSCURA_BIN` 環境變數。

## 普通文字接話

`conversation.continuation_enabled` 預設 true；`continuation_window_seconds` 預設 120，且限當前訊息之前最近 12 則。只有這個窗口有 Bot 發言的普通訊息才成為分類候選。睡眠、明確 @／名字／reply 與複讀沿用原有優先序。

`model.attention` 留白沿用 chat，可設獨立輕量模型；`attention_max_output_tokens=512`。使用者已選只關閉分類推理：`attention_reasoning_enabled=false`（舊配置未填也採false），只在attention請求依服務格式關閉推理（Go DS：`thinking.type=disabled`；OpenRouter：`reasoning.enabled=false`），不改聊天、背景記憶、任務或視覺的推理參數。

`attention_reasoning_enabled`只接受TOML布林值，不能填`"false"`或0／1。設true時才套用`attention_reasoning_effort`（預設low）；effort留空則送`reasoning.enabled=true`。這個開關只影響分類，並非聊天模型的全域推理開關。接入與驗證見[交付](handoff/ATTENTION-INTEGRATION-DELIVERY.md)。

`conversation.continuation_timeout_seconds=8`仍維持；逾時、API 失敗或無效判斷會安靜結束，沒有強制回覆備援。這只增加普通接續候選的分類，不為每個聊天回合增加前置模型。結果與失敗原因記錄於 participation trace。

`conversation.cross_channel_window_seconds=600`：在已授權共享頻道內，明確接續線索或「我們去 <#頻道ID> 聊」可帶入近期原文；普通短句只有同一作者已邀請轉場才跨頻道觸發。依賴即時快取，不保證重啟恢復。來源區塊最多4000字元、且不超過history_chars一半，從近期歷史空間扣除；權限、來源內容與privacy在送達前重查。詳見[轉場交付](handoff/CROSS-CHANNEL-CONTINUATION-DELIVERY.md)。

## 自身記憶載入預算（2026-10-05）

`memory.self_chars` 預設2000，允許400–20000；計算完整self_memory JSON字元，不是tokens。`memory.self_limit` 預設12，允許1–100。兩項適用聊天編譯及MEMORY_SEARCH，舊配置未填使用預設。來源仍須符合既有 `shared_channel_ids` 與當前讀取權限；這些上限不刪除資料。省略量與掃描上限另揭露。詳見[DATA](DATA.md)。


## RC 背景整理與每日額度

新的RC範本 `memory.background_enabled=true`；明確設定false或未填此欄的舊配置仍維持關閉。它在已處理的聊天回合後排程，不在普通聊天前多加模型。每頻道40則新訊息、至少600秒間隔、每批至多100則／每則1200字元；200則快取積壓按ID由舊到新分批，不跳過前100則。超過即時快取且未進入job的原文仍保留可搜尋，但不宣稱無限歷史自動回補。重啟pending工作須等新的合格Bot觸發才恢復；最多3次抽取嘗試，截斷不寫半份JSON。

`background_daily_calls=20`依UTC日計算usage中的memory嘗試。啟用備援時，排程保守為每個活動／新job預留2次，防止最後一個名額因備援變成超額；因此可能留下1次未使用額度，即使備援key暫缺也會預留。routes模式每端SDK不重試；舊單服務配置若自行啟用SDK隱藏重試，HTTP次數不能僅由usage列數推定。每頻道仍只有一個背景job，不阻塞前景回覆。

背景writer保留作者名稱、ID、Bot自身標記、reply與原文時間；Bot的RP只能供摘要理解，不能從這個入口寫成一般人物畫像或永久經歷。evidence逐字複製單一來源片段，問句等其他依據放當批evidence_ids；不做OpenCC或以改寫片段蒙混驗證。來源變動、權限、隱私與修訂在實際寫入前重查。
