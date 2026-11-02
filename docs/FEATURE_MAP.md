# 功能位置索引

## 正式功能從哪裡找

本表是目前RC的維護入口；下方研究歷史保留追溯，不代表仍待採用。`evals/prototypes`不由正式Bot載入；歷史子代理只有明確安裝研究wrapper才存在，沒有正式tool_delegate用途。

| 功能層 | 正式位置 | 責任與設定 |
|---|---|---|
| 啟動／配置 | `app.py`、`config.py` | 配置root、憑證名稱、啟動檢查；不共用舊庫 |
| 模型主備 | `llm/routing.py`、`llm/client.py` | chat／light／vision獨立主備、原生推理參數、期限／取消、用量與failover log；[配置](MODEL_ROUTES.md) |
| Discord接收／管理 | `discord/bot.py`、`events.py`、`access.py`、`admin.py`、`tasks.py` | 事件入庫、身份／權限、指令與本人恢復同意 |
| 參與判斷 | `conversation/attention.py`、`participation.py` | @／睡眠／複讀／主動查看；普通接話才走輕量分類 |
| 上下文與轉場 | `context/compiler.py`、`snapshots.py`、`completeness.py`、`cross_channel.py` | 出列快照、reply補讀、來源作者／self、工具等待後到訊息、共享轉場 |
| 回合與接續 | `conversation/engine.py`、`models.py`、`continuation.py`、`verification.py`、`repair.py` | 每頻道佇列、實際查證、後到要求、有界修復／收尾、防重做 |
| 工具 | `tools/registry.py`、`protocol.py`、`executor.py`及各領域模組 | 單一登錄衍生說明／驗證，格式解析及真實執行 |
| 發送／等待 | `delivery/service.py` | 分段人工等待、指定回覆、實際送達回執 |
| 記憶 | `memory/service.py`、`profiles.py`、`evidence.py`、`self_memory.py` | 來源、更正圖、情境、近期原文、固定自身核心 |
| 背景／治理／匯入 | `memory/background.py`、`governance.py`、`migration.py` | 批次與額度、逐字片段、opt-out／清除／本人恢復、舊格式映射 |
| 儲存與單例 | `storage/database.py`、`instance_lock.py` | schema4、30天診斷、收據、device/inode別名鎖；[資料](DATA.md) |
| 開發驗證 | `evaluation/`、`tests/`、`scripts/`、`evals/` | 模擬／比較／證據，不是每回合的執行架構 |

目錄均相對`src/sayuki_bot/`（開發驗證列除外）。[文件總索引](INDEX.md)、[RC交付](handoff/RC-COMPLETION.md)與下方細項提供實測範圍。

## 歷史研究與細項位置


無研究鏈的時間複驗集中在 `scripts/message_clock_formal_live.py`（capture／live／audit／review），沿用時間wrapper及child bridge，強制研究旗標false，核對實際payload／trace無研究投影。`tests/test_message_clock_formal.py`、`check_message_clock_formal_mutations.py` 保護邊界；40離線／40真實情境、42次Go與完整wire契約通過，語意未見改善，正式不載入。`paired-findings`另列多解時鐘旁白與答對時段後的錯誤事件推論，避免通用quality欄位遮掉附加矛盾。見[交付](handoff/MESSAGE-CLOCK-FORMAL-VALIDATION.md)。

發話時刻AM／PM研究：`evals/prototypes/message_clock_ampm.py` 只轉編譯器已定型的訊息時間／第二系統標籤，保留日期偏移與原文；`message_clock_ampm_live.py` 隔離入口，`analyze_message_clock_ampm.py` 獨立驗證可逆時間與實際來源，`review_message_clock_ampm.py` 依送達分評一般／時段問題／額外clock句。`check_message_clock_formal_compatibility.py` 證明無研究鏈也能單獨離線運作。40次Go有局部訊號仍有反例，正式不載入；見[交付](handoff/MESSAGE-CLOCK-AMPM-PROTOTYPE.md)。

時間診斷後續模型結果見 [Go對照交付](handoff/TEMPORAL-CHAIN-LIVE-RESULTS.md)：`analyze_temporal_chain_overhead.py` 核對同前綴首輪tokens，`review_temporal_chain.py` 將實際一般建議與直接時段探測分評。56情境／56次Go；來源齊全仍有錯接、候選不接入。來源／時間欄位／規則原型仍只在eval child中載入。

時間混淆診斷位於 `scripts/diagnose_temporal_chain.py`（舊序列完整SDK核對）及 `evals/prototypes/temporal_chain_layout.py`（時鐘遮蔽僅診斷／鄰接祖先原文候選）。`temporal_chain_live.py` 以獨立child bridge載入，不改共享worker；`analyze_temporal_chain.py` 核對實際來源、邊、送達與宣告差異，`review_temporal_chain.py` 將一般建議與直接確認時段分開。10合成模板／56情境目前僅離線，Go外傳待本批核准；正式不載入。[交付](handoff/TEMPORAL-CHAIN-DIAGNOSIS.md)。

TOOL_HELP假待辦正式解除：`conversation/repair.py:release_diagnostic_help`／`focus_messages` 保留原生／標準名稱線索與真實查證條件，`engine.py:run` 綁trace回呼；`test_tool_help_relief_integration` 驗正式入口，`replay_native_tool_help_relief.py`／`analyze_native_help_relief.py` 驗封存舊版＋原型與native逐輪等同，`check_tool_help_relief_mutations.py --native` 做正式碼負控制。`pre_tool_help_relief.py`只供歷史測試，不屬正式套件。[正式交付](handoff/TOOL-HELP-RELIEF-INTEGRATION.md)。

TOOL_HELP診斷待辦解除原型：`evals/prototypes/tool_help_pending_relief.py` 在實例函式globals副本替換RepairState工廠，保留正式Engine.run code object；`replay_tool_help_pending_relief.py` 用獨立child入口回放既有模型回應，`analyze_tool_help_relief.py` 重讀帳本／實際wire／工具／Delivery，`check_tool_help_relief_regression.py`／`check_tool_help_relief_mutations.py` 驗邊界與負控制。測試 `test_tool_help_pending_relief`；[交付](handoff/TOOL-HELP-PENDING-RELIEF-PROTOTYPE.md)，正式Bot不載入。

回覆鏈原文組研究：`evals/prototypes/reply_chain_quotes.py` 只從已編譯可見資料沿reply_to回溯；保留ID／作者／原文，揭露缺失、縮短、衝突、循環與額度，不補讀或推時間。`reply_chain_live.py`／`analyze_reply_chain.py`／`review_reply_chain.py` 管理隔離模型入口、SDK可見來源逐字核對及人工時序／人物／話題評閱；`test_reply_chain_quotes`、`test_reply_chain_review` 與7項變異保護資料和證據邊界。worker的 `deleted_after_prior` 走正式刪訊息handler，控制來源失效。`probe_reply_chain_repair.py` 重播正式parser／RepairState及既有完整入口序列，定位TOOL_HELP假待辦，並以完整SDK body核對已刪原文缺席；不發API、不修正式碼。[交付](handoff/REPLY-CHAIN-QUOTES-PROTOTYPE.md)；正式Bot不載入。

時間欄位說明研究：`evals/prototypes/event_time_roles.py` 只在評估模型實例的chat輸入加固定說明及改標籤，保持所有來源文字／時間值、模型用途／階段與輸出；正式Bot不載入。`event_time_roles_live.py`／`analyze_event_time_roles.py`／`review_event_time_roles.py` 分開真實目標、實際READ、時序輸入與人工時間標註。worker的可選 `read_fixtures` 只供假Discord fetch，原文不手餵歷史。`test_event_time_roles` 及評閱測試保護輸入可逆、用途隔離及證據；[本批不建議採用](handoff/EVENT-TIME-ROLES-PROTOTYPE.md)。

時序邊界控制：`evals/queued-controls-scenarios.json` 定義未答口味／新細節、真正再次問口味／時間；`scripts/queued_controls_live.py` 用受控前綴與真實目標回覆。共用worker的 `controlled_main_responses` 只在主回合本機回應，`after_settle` 以實際Delivery ID接新來話；`analyze_queued_controls.py` 核對前綴、active狀態及來話／送達／生成順序，`review_queued_controls.py` 分開人工答案覆蓋與附帶時間錯誤。`test_queued_controls`／`test_queued_controls_review` 防止假覆蓋、假來源、只改模式標記即冒充真實評閱；正式Bot不載入。[交付](handoff/QUEUED-CHRONOLOGY-CONTROLS.md)。

新人物／旁支驗收：`evals/attribution-side-scenarios.json` 凍結八案，`scripts/attribution_side_live.py` 重用既有隔離接收入口；`analyze_attribution_side.py` 驗作者、reply、真實先前送達與排隊回合可見性，`review_attribution_side.py` 核對人工標註與實際送達。時序研究僅在 `evals/prototypes/queued_turn_chronology.py`，worker收到明示flag才安裝；`queued_chronology_live.py`／`analyze_queued_chronology.py` 保存有界呼叫與實際入列／送達／生成順序，正式套件不載入。[交付](handoff/ATTRIBUTION-AND-QUEUE-CHRONOLOGY.md)。

明確讀取命令：`conversation/verification.py` 的短寫命令與子句否定規則，沿用既有 `ContinuationState` 登錄與優先來源。`test_literal_read_requests` 走正式接收／SQLite／工具／Delivery，`check_literal_read_mutations.py` 在暫存副本驗正常控制及斷言失敗。`analyze_literal_read_followup.py` 核對實際 READ 與 continuation packet，`test_literal_read_evidence` 防止把聲稱成功、錯人或歷史讀取當成新查證。[修復交付](handoff/LITERAL-READ-REQUEST-DELIVERY.md)。

十案真實基線的證據與診斷：`scripts/review_read_reply_baseline_quality.py` 核對 20 筆人工標註與實際主／其他回合的送達、規則與帳本；`scripts/probe_read_reply_baseline_gaps.py` 重放正式查證解析／狀態，另在新 SQLite 重建兩筆自然 event 的原文投影及撤回。`test_read_reply_baseline_evidence` 四項保護觀測歸屬與來源記憶，沒有 API 呼叫或正式 monkeypatch。[真實基線交付](handoff/READ-REPLY-BASELINE-LIVE.md)。

十案正式版驗收入口：`scripts/read_reply_baseline_worker.py` 將歷史、實際先前送達及 fetch 等待中的來話分階段接入正式 Bot；`read_reply_baseline_live.py` 負責隔離父子程序、Go 傳輸、上限與永久帳本，`--capture-only` 不讀金鑰。`analyze_read_reply_baseline.py` 分開輸入契約／情境覆蓋／engine outcome；語意仍人工評閱。`test_read_reply_baseline` 為 12 項端到端離線與控制測試。[入口交付](handoff/READ-REPLY-BASELINE.md)。原十案 JSON 保留其設計時狀態，實作與執行狀態以本交付為準。

讀原文後的群友感驗收：`evals/read-reply-calibration-rubric.json` 分開自然意願確認、來源釐清與直接接辦舊任務；`scripts/review_read_reply_calibration.py` 核對人工標註、報告與永久帳本，只計數、不自動判斷語意。`test_read_reply_calibration` 覆蓋完整性與混合句計數。`evals/read-reply-calibration-scenarios.json` 是尚未執行的十案規格，未接入 worker；[校準交付](handoff/READ-REPLY-CALIBRATION.md)。正式 Bot 不載入這些評閱資料。

初始讀原文後的追加定位已回退：正式 `conversation/engine.py` 回到接入前，`conversation/read_reply.py` 不存在。歷史接入只由 `evals/prototypes/archived_read_reply_integration.py` 從 SHA 固定封存載入私有研究模組，`read_reply_integration_live.py` 標成歷史用途；`test_read_reply_rollback`／`check_read_reply_rollback.py` 核對正式模組與每輪 wire。`check_profile_source_wheel.py` 也檢查完整模組集合，拒絕多出舊 helper。[回退交付](handoff/READ-REPLY-ROLLBACK.md)。

完成狀態替換只在研究模式 `read_completion_state` 安裝：`evals/prototypes/read_completion_state.py` 替換唯一的過期初始查證指令尾段；`read_completion_state_live.py` 與獨立重複 runner 由父程序核對精確尾段還原，`analyze_read_completion_state.py` 核對實際 SDK／帳本。新批次沒有改善、不採用；[研究交付](handoff/READ-COMPLETION-STATE-PROTOTYPE.md)。

成功讀原文後的回答定位（未採用原型）：`evals/prototypes/read_reply_goal.py` 使用原生 Verification 的實際完成狀態，於評估實例 `Engine.generate_reply` 補原始要求 packet；不改 formal compiler，排除後到訊息、特殊用途、拒絕與超預算。worker `read_reply_goal` mode 才安裝；`read_reply_goal_live.py` 要先有成功 keyless 捕捉，父程序精確移除一組追加內容核對來源；`analyze_read_reply_goal.py` 交叉核對成功時機、frame、SDK 與帳本。[交付及採用決策](handoff/READ-REPLY-GOAL-PROTOTYPE.md)。

發版準備與隔離對照：`scripts/check_installation.py --host-only` 為不依賴 Bot 安裝的主機盤點；`scripts/capture_model_roles.py` 離線觀測正式 ModelClient 的 7 用途／階段；平台步驟在 [DEPLOYMENT_PREFLIGHT](DEPLOYMENT_PREFLIGHT.md)。`evals/prototypes/current_turn_goal.py`／worker `current_turn_goal` mode 只在研究時補對話目標，`current_turn_goal_live.py` 及 `analyze_current_turn_goal.py` 核對來源不變與帳本；**沒有接入正式 compiler**。見[第一批交付](handoff/RELEASE-BATCH-1-DELIVERY.md)。

最後工具接續低推理已正式接入：`conversation/engine.py` 的 `generate_terminal_reply()` 明示 `final_tool`／`closeout`；`llm/client.py` 的 `supports_final_tool_reasoning()` 限定已驗證的 Go DS V4.1 路由，`ModelClient.generate(..., phase=...)` 只在 chat 目標階段送 native `reasoning_effort=low`，普通／早期、0 工具回合、task 及其他用途保持原設定。`model_generation_phase` trace 記錄實際設定及輸入 hash。`test_final_reasoning_integration` 以封存舊 Engine＋原型核對完整 SDK／送達等同；`final_reasoning_native_live.py`／`analyze_final_reasoning_native.py` 無原型跑正式模擬並核對帳本。[正式交付](handoff/FINAL-REASONING-INTEGRATION.md)。以下原型為研究歷史。

最後工具接續低推理原型：`evals/prototypes/final_tool_reasoning.py`在實際`Engine.generate_terminal_reply`及ModelClient role／turn範圍內，用ContextVar限定原生Go low；不靠prompt或SDK次數判斷，不改正式配置。`group_memory_worker.py`的`final_tool_reasoning`顯式模式才安裝；`final_tool_reasoning_live.py`提供基線／候選交錯與父程序真實wire記錄，`analyze_final_tool_reasoning.py`跨child階段、SDK及永久帳本核對。`test_final_tool_reasoning`與`check_final_tool_reasoning_mutations`覆蓋重試、其他角色／turn、取消與還原。[交付與未採用狀態](handoff/FINAL-TOOL-REASONING-PROTOTYPE.md)。

聊天推理診斷：`scripts/diagnose_chat_reasoning.py`只在eval父程序對真正SDK請求送DeepSeek原生low／thinking-disabled，不改ModelClient或正式設定；`scripts/analyze_chat_reasoning.py`核對永久帳本、來源、首輪相同輸入與空白分類，Delivery語意另評。`test_chat_reasoning_diagnostic`／`check_chat_reasoning_mutations`覆蓋實際wire、session、額度、取消與空白歸因。[實測結果與不全面調整建議](handoff/CHAT-REASONING-DIAGNOSIS.md)。

現行[工具上限有界收尾已正式接入](handoff/TERMINAL-CLOSEOUT-INTEGRATION.md)：`conversation/closeout.py`負責完整狀態／終止指令／協定篩選，`Engine.generate_terminal_reply`只在最後原生成真的被工具上限拒絕後追加一次，仍走原Delivery與查證；不排程、不執行額外工具。`terminal_integration_live.py`提供OpenCode Go父程序代理，`analyze_terminal_integration.py`核對永久帳本、SDK／trace及實際送達，語意另人工評閱。`test_closeout_integration`／`test_opencode_go_transport`與`check_terminal_integration_worker`／`check_closeout_integration_mutations`提供驗證。歷史原型測試／worker用`pre_terminal.py`鎖到上一批封存Engine；正式Bot不匯入。下列收尾原型描述保留為研究歷史，未採用普通回覆latest-priority指令。

投票終止狀態的後續實測已完成：[7案模擬結果與未接入交付點](handoff/TERMINAL-RECOVERY-LIVE-RESULTS.md)；原型仍只在評估實例安裝。候選6筆投票未見等待承諾；2筆API逾時分別列失敗，跨批SDK／帳本／送達證據由`analyze_terminal_recovery.py`核對。

投票終止狀態研究：`evals/prototypes/terminal_recovery.py`只調整既有cap-recovery的收尾SDK輸入，保留完整未執行清單；`rare_tool_live.py --delegate-kind terminal_recovery`／`group_memory_worker.py`顯式安裝。`analyze_terminal_recovery.py`與共同分析器核對實際新輸入及獨立trace，`check_terminal_recovery_*`提供worker／變異。正式套件不匯入；[離線完成、模型補測待授權](handoff/TERMINAL-RECOVERY-PROTOTYPE.md)。

路徑相對 `src/sayuki_bot/`；非入口實作不應直接啟動獨立 Bot。

事後上限收尾研究：`evals/prototypes/cap_recovery.py`，原最後一輪不預先改提示；有實際工具輸出且符合正式cap拒絕條件才多一次同模型文字收尾，保留完整未執行清單、最新來源與原期限／保護。`rare_tool_live.py --delegate-kind cap_recovery`／`group_memory_worker.py`顯式安裝；`analyze_cap_recovery.py`／`check_cap_recovery_*`分開實際拒絕、SDK與送達證據。正式套件不匯入；[結果與未接入限制](handoff/CAP-REJECTION-RECOVERY-PROTOTYPE.md)。

工具上限收尾研究：`evals/prototypes/tool_cap_closeout.py`，只在最後既有生成附零工具額度指令及實際狀態。`rare_tool_live.py --delegate-kind cap_closeout`／`group_memory_worker.py`提供明確評估模式，兩邊共用上批未接入latest-priority；`analyze_tool_cap_closeout.py`、`check_tool_cap_closeout_*`及`replay_tool_cap_closeout.py`分開輸入、品質與原樣失敗重播。正式套件不匯入；[建議不接入與反例](handoff/TOOL-CAP-CLOSEOUT-PROTOTYPE.md)。

連續改口最新優先研究位於 `evals/prototypes/latest_reply_priority.py`；只在正式狀態完整、查證已有結果且多則後到時替換最後回答定位。評估入口 `rare_tool_live.py --delegate-kind latest_priority`／`group_memory_worker.py`；分析 `analyze_latest_reply_priority.py`、對照／變異 `check_latest_reply_priority_*`、共同上限診斷 `probe_latest_priority_round_cap.py`。正式套件不匯入；[7/8送達與未接入限制](handoff/LATEST-REPLY-PRIORITY-PROTOTYPE.md)。

時間原型R2位於 `evals/prototypes/temporal_delivery.py`；評估入口的 `temporal_delivery`／`temporal_delivery_smoke` 明確啟用，正式套件不匯入。涵蓋本次主頻道待送達台詞與原文召回，見[覆蓋與限制](handoff/TEMPORAL-DELIVERY-R2.md)。
時間斷言實驗在 `evals/prototypes/temporal_rewrite.py`，`scripts/group_memory_worker.py`／`rare_tool_live.py` 僅提供明確的評估模式；正式套件不匯入，見[原型結果](handoff/TEMPORAL-REWRITE-PROTOTYPE.md)。
明確原訊息查證：`conversation/verification.py`／`engine.py`，已知msg_ID定位：`tools/executor.py:message_channel`／`discord_queries.py`；投票事實回傳：`tools/social_actions.py`／`registry.py`。測試 `test_verification_contract`／`test_verification_scope`；[契約與限制](handoff/VERIFICATION-CONTRACT-DELIVERY.md)。

工具途中後到查證與優先回答：`conversation/continuation.py`、`models.py:Turn.continuation`、`engine.py:run/generate_reply`。只在原有merge邊界增加同作者明確待查；共享原Verification、保留原訊息身分及最新`priority_source_id`，不吞掉訊息本身的回合。測試`test_continuation_integration`／`test_continuation_analysis`，變異`check_continuation_mutations.py`；[正式接入與模型觀測](handoff/CONTINUATION-STATE-INTEGRATION.md)。自身字串解釋原型尚未接入。

空正文驗證與有限重試：`llm/client.py`、`conversation/engine.py`、`conversation/models.py`；形狀／null評估留存：`evaluation/live.py`、`evaluation/context_live.py`。測試 `test_empty_completion`／`test_empty_worker`；[交付與限制](handoff/EMPTY-REPLY-AND-MEMORY-GUIDANCE-DELIVERY.md)。記憶指引候選只在 `evals/prototypes/memory_guidance.py`，未接入正式compiler。

跨版本實際功能、縮減參數、未覆蓋的平台行為見 [O6-A](handoff/O6-A-FEATURE-AUDIT.md)；本表只回答入口位置，不能代替能力驗收。

| 功能 | 入口／主要實作 | 配置與資料 | 驗證 |
|---|---|---|---|
| 安裝、啟停與本機檢查 | `app.py`、`__main__.py`；專案根`pyproject.toml`、`uv.lock`、`scripts/check_installation.py`、`scripts/smoke_installed.py` | `config.py`、instance lock；[安裝說明](INSTALLATION.md) | O8獨立環境／wheel、`tests/test_o8_installation.py` |
| Discord 訊息與修改／刪除 | `discord/bot.py` | events、記憶失效 | core source invalidation tests；O5-C1 正式入口重現（`tests/test_conversation_data_path.py`） |
| 管理指令 | `discord/admin.py` | admin_ids、trusted_guild_ids | 本機 command tree 組裝 |
| @、名字、reply、複讀、主動查看 | `conversation/attention.py` | sleep、conversation 配置 | PolicyTests；O5-C1 `C07AttentionReasonTests`（閘門與睡眠邊界通過；C2 已補中文原因與本事件命中事實） |
| 不用 @ 的普通文字接續 | `conversation/participation.py`、`attention.py`、`engine.py` | continuation_enabled/window_seconds/timeout_seconds、model.attention；只對近期候選分類 | `test_ordinary_continuation`；[正式入口與真實模擬](handoff/ORDINARY-CONTINUATION-DELIVERY.md) |
| 接話分類獨立推理開關 | `llm/client.py`、`config.py` | model.attention_reasoning_enabled=false；512輸出額度／8秒期限維持 | `tests/test_attention_reasoning.py`；[接入交付](handoff/ATTENTION-INTEGRATION-DELIVERY.md) |
| 接話逾時的隔離診斷 | `evals/prototypes/attention_diagnostic.py`、`scripts/diagnose_attention_live.py`／`probe_attention_bridge.py`／`report_attention_diagnostic.py` | 合成分類records、獨立60秒觀測及8秒標記；無正式設定變更 | `tests/test_attention_diagnostic.py`；[診斷結果](handoff/ATTENTION-TIMEOUT-DIAGNOSIS.md) |
| 跨頻道接續／明確邀請轉場 | `context/cross_channel.py`、`discord/bot.py`、`discord/access.py`、`conversation/participation.py` | cross_channel_window_seconds、既有shared_channel_ids；原文引用與送達前重查 | `test_cross_channel_continuation`；[轉場交付](handoff/CROSS-CHANNEL-CONTINUATION-DELIVERY.md) |
| 每頻道排隊、取消、生成與工具接續 | `conversation/engine.py` | max_queue、max_tool_rounds | FlowTests；C2 出列時更新快照，工具接續有界補充；`tests/test_context_updates.py` |
| 分段、等待、指定回覆及發送 | `delivery/service.py` | delivery 配置、receipts；已移除OpenCC自動轉換 | FlowTests、ProtocolTests、NoConversionTests |
| 模型連接、推理設定、用量 | `llm/client.py` | model 配置、usage | ModelTests、OpenRouter 合成案例 |
| 角色卡與工具教學組裝 | `context/compiler.py`、`tools/registry.py` | paths.persona、tools.compact；分類目錄＋常用完整格式 | ContextTests、`tests/test_tool_teaching.py` |
| 初始原文補全與候選問答 | `context/completeness.py`、`context/compiler.py` | 本頻道24小時／1,000候選；有界reply／鄰近原文，無模型前置步驟 | `test_context_completeness`；[M8](handoff/GROUP-MEMORY-M8-DELIVERY.md) |
| 上下文窗口、原文和摘要 | `context/compiler.py` | history_limit、history_chars、summaries | ContextTests；O5-C1 確認逐則不截斷、冷啟動取最近 N 則；C2 已補自身標記、reply 與省略揭露 |
| 直接來源補讀、快照更新與新增訊息 | `context/snapshots.py`、`context/records.py` | reply_source_chars、reply_fetch_timeout_seconds、tool_merge_chars/messages | C1 缺口回歸與 C2 source lifecycle／merge tests |
| 工具名稱／參數／說明 | `tools/registry.py` | registry 為單一來源 | 產生 TOOLS.md |
| 控制標記解析 | `tools/protocol.py` | 無獨立設定 | ProtocolTests |
| 工具執行 | `tools/executor.py` 分派至 `discord_queries.py`、`social_actions.py`、`memory_tools.py` 等領域 | registry、結果上限 | 個別來源測試與後續 Discord 實測 |
| 有界頻道翻頁／主題與資產子指令查找 | `tools/discord_lookup.py`、`tools/registry.py` | 共享設定、每頁與字元上限 | O6-B query／receive tests |
| 工具結果完整 JSON／省略揭露 | `tools/results.py`、`conversation/engine.py` | tools.max_result_chars | ReportBudgetTests、O6-B 正式入口 |
| Discord 存取與動作授權 | `discord/access.py` | shared_channel_ids、admin_ids | AccessTests |
| 貼圖來源／媒體元資料 | `discord/stickers.py`、`discord/events.py`、`tools/stickers.py` | 既有 events.attachments，無 schema 變更 | O6-D 來源／隱私／刪除案例 |
| 有界圖像解碼／描述快取 | `tools/media.py`、`tools/stickers.py` | Pillow；貼圖描述128筆、5分鐘 | O6-D 格式／大小／快取／取消案例 |
| 貼圖發送 | `tools/social_actions.py`、`delivery/service.py`、`discord/access.py` | 本群可用貼圖；沿用 receipts | O6-D 正式入口成功與 unknown |
| 網頁搜尋與讀取 | `tools/search.py`、`safe_web_proxy.py` | obscura_bin | search lifecycle、safe proxy tests |
| 數學計算／圖像 | `tools/math_tools.py` | 程式內有界工作限制 | math tests |
| 文字編碼／雜湊 | `tools/text_transform.py` | 有界輸入輸出 | text_transform tests |
| 近期事件自動選入 | `context/relevance.py`、`context/compiler.py` | 當前文字／可見 reply 的交集、人物範圍、既有 profile_chars；event_selection trace | `test_o4bb_relevance.py`、`test_o4bb_receive.py`；[交付](handoff/O4B-B-DELIVERY.md) |
| 搜尋命中的reply來源鏈 | `memory/search_context.py`、`memory/service.py:search_context`、`tools/memory_tools.py` | events唯讀、2層／8則／4,000字元、現有工具總上限；`event_reply`索引 | `test_search_context_production`；[M7](handoff/GROUP-MEMORY-M7-DELIVERY.md) |
| 近期事件原文／索引分離、明確來源 | `memory/service.py`、`tools/memory_tools.py`、`tools/registry.py`、`context/compiler.py`、`context/relevance.py`、`memory/background.py` | event 必填 msg_ID；摘要僅供檢索；原文／來源時間與作者、失效、2,000字元上限 | `test_event_memory_source`；[交付](handoff/EVENT-SOURCE-DELIVERY.md) |
| 記憶統一更新與查找 | `memory/service.py` | memories、events、範圍 | MemoryTests |
| profile明確較早來源與其reply依據 | `tools/registry.py`、`tools/memory_tools.py:specified_source`；沿用`memory/service.py`與`profiles.py` | 實際作者／時間／頻道、memory_sources；不增加模型或schema | `tests/test_profile_source.py`；[修復交付](handoff/PROFILE-SOURCE-DELIVERY.md) |
| 身分不明時暫緩記憶 | `tools/registry.py`、`memory/background.py` | 工具教學與參數回饋；等明確 @／訊息身分，不新增名字查找 | `tests/test_memory_identity.py`；[驗證](handoff/O4A-IDENTITY-DELIVERY.md) |
| 本人陳述／轉述／長期印象 | `memory/evidence.py`、`service.py`、`tools/memory_tools.py` 的 MEM_OBSERVE | schema 3：basis、memory_sources，最多八則來源 | `tests/test_memory_impressions.py`，真實模型分類 smoke |
| 畫像更正、情境與跨頻道解析 | `memory/profiles.py`，由 `memory/service.py` 統一寫入 | memories.context、profile_revisions | ProfileRevisionTests、CrossChannelExtractionTests |
| 自身偏好／對話經歷 | `memory/self_memory.py`、`memory/service.py:self_context`、`tools/memory_tools.py` | self_memory_meta、memory_sources；self_chars／self_limit；固定核心無寫入類別 | `test_self_memory_policy`、`test_self_memory_worker`；[交付](handoff/SELF-MEMORY-DELIVERY.md) |
| 背景 reply 與時效一致性 | `memory/background.py` | 持久化工作恢復 reply、生成後再查到期、舊摘要來源時間 | `tests/test_o4ba_consistency.py`；[交付](handoff/O4B-A-DELIVERY.md) |
| 背景摘要與人物抽取 | `memory/background.py` | memory 配置、usage 計費 | 預設關閉；合成模型抽取案例與 ExtractionTests |
| 有來源的畫像更正工具 | `tools/memory_tools.py`、`tools/registry.py` 的 MEM_REVISE | 按目前可讀條目 ID 更正 | 正式工具路徑與跨頻道合成案例 |
| 提醒排程與恢復 | `conversation/reminders.py`、`tools/social_actions.py` | reminders、receipts | 睡眠延到清醒已確認；出列再查；Discord 實測待補 |
| 按鈕接續與原指定對象 | `discord/interactions.py`、`tools/social_actions.py`、`discord/bot.py`、`context/compiler.py` | 全群每人一次；固定10分鐘，重啟不恢復 | O6-C 22 項離線；Discord 實測待補 |
| SQLite schema、交易與診斷 | `storage/database.py` | paths.database | MemoryTests、migration tests |
| 舊記憶離線匯入 | `memory/migration.py`、`scripts/import_memory.py` | 顯式來源／目的範圍 | MigrationTests |

所有工具的個別名稱和用法見 [TOOLS](TOOLS.md)。不要為新增工具再另建 names／required arguments 清單。

圖片描述快取：`storage/database.py:observation/save_observation`；圖片讀取：`tools/discord_queries.py`；資料匯出與清除：`memory/governance.py`；動態信任名單：`discord/admin.py` 與 `discord/access.py`。

實測用量報告：`evaluation/report.py`、`scripts/evaluation_report.py`；收集與比較方式見 `evals/README.md`。

無 Discord 模擬入口：`evaluation/simulator.py`、`scripts/simulate.py`；範例：`evals/simulation-example.json`。

4.0／4.6 離線組裝對照：`evaluation/comparison.py`（4.6 側與報告）、`evaluation/legacy_v4.py`（4.0 側，隔離子程序）、`scripts/compare_versions.py`；共同案例：`evals/comparison-suite.jsonl`，回歸變體：`evals/comparison-regression.jsonl`；驗證：`tests/test_comparison.py`。

4.0／4.6 共同流程重播（E 層）：`evaluation/flow.py`（4.6 側、共同時間軸與報告）、`evaluation/legacy_v4.py` 的 `run_flow_cases`（4.0 側，真正 Scheduler）、`scripts/compare_flows.py`；流程案例：`evals/flow-suite.jsonl`；驗證：`tests/test_flow_comparison.py`。

4.0／4.6 真實模型成對比較：`evaluation/live.py`（傳輸、預算、成對排程、盲評稿）、`evaluation/flow.py:run_live_comparison`、`scripts/compare_live.py`；驗證：`tests/test_live_comparison.py`。

合成 Discord 物件與工具範圍：`evaluation/environment.py`；跨頻道、來源存在狀態、圖片與時間案例：`evals/README.md`；批次模型評估：`scripts/evaluate.py`。

C2 前後正式接收端的真實模型比較：`evaluation/context_live.py`（隔離／取消 bridge）、`scripts/context_live_worker.py`（正式接收端 worker）、`scripts/compare_context_live.py`（固定 Modal、AB／BA、永久帳本）；案例 `evals/context-live-suite.json`，驗證 `tests/test_context_live.py`。

C3 唯讀支援：`scripts/context_live_preflight.py`（公開路由與測試額度查核）、`scripts/verify_context_live.py`（既有 generation metadata，不送 completion）、`scripts/report_context_live.py`（本機統計與分檔閱讀稿）。結果見 [C3 交付](handoff/O5-C3-DELIVERY.md)。

人物畫像語意／來源探測：`scripts/probe_memory_semantics.py`，走正式前景接收與背景抽取入口，完全離線；[調查與待定政策](handoff/O4A-MEMORY-SCOPE-PLAN.md)。固定壞提案被接受是缺口觀測，不計作驗收通過。

O6-E 新增定位：

| 功能 | 主程式 | 驗證 |
|---|---|---|
| 專用任務入口／取消 | `discord/tasks.py`、`conversation/engine.py`、`llm/client.py` | test_o6e_tasks、test_o6e_receive |
| 輔助分段取消／目的權限 | `tools/executor.py`、`discord/access.py`、`delivery/service.py` | test_o6e_delivery |
| 提醒睡眠出列／unknown | `conversation/reminders.py`、`conversation/engine.py` | test_o6e_delivery、test_reminders |

O6-E2 分項驗收與來源：[社交交付矩陣](handoff/O6-E2-DELIVERY.md)。`tools/social_actions.prepare` 負責有上下文的無副作用預檢；純參數規則仍在 registry；`protocol.auxiliary_parts` 共用文字契約；`discord_queries` 處理投票票數不確定性。

舊機制核對：`scripts/audit_legacy_mechanisms.py` 產生三版工作目錄符號／匯入／雜湊索引；[處置與退出條件](handoff/LEGACY-MECHANISM-AUDIT.md)。`tests/test_legacy_cleanup.py` 驗搜尋設定與失敗提示；數學測試直接驗正式解析器。

多人記憶研究基線：`scripts/probe_group_context.py` 經真正接收端量測不同歷史視窗的來源覆蓋；使用離線SDK，不能衡量回答品質。[設計與待實作範圍](handoff/GROUP-MEMORY-DESIGN.md)。

M2多人記憶原型（不屬正式套件）：`evals/prototypes/group_context.py` 包含來源視圖、回覆鏈、短期提議與背景工作；`scripts/probe_group_memory.py` 跑170次共同資料預算對照；`tests/test_group_memory_prototype.py` 驗契約／失效／取消，`scripts/probe_group_memory_mutations.py` 驗負控制。[交付與M3界線](handoff/GROUP-MEMORY-M2-DELIVERY.md)。

M3真實比較：`scripts/group_memory_live.py` 管父程序、按案例正反順序與追加帳本；`scripts/group_memory_worker.py` 跑無金鑰接收端與Writer；`scripts/report_group_memory_live.py` 核對設定／用量／空回覆並產生閱讀稿；`evals/group-memory-m3-suite.json` 為合成場景。驗證在`tests/test_group_memory_live.py`，[M3交付](handoff/GROUP-MEMORY-M3-DELIVERY.md)區分資料缺口、Writer截斷與角色擴寫。

M4：`evals/prototypes/group_neighbors.py` 包裝C並加入有界周邊原文；`scripts/group_response_shape.py` 記SDK結構而不記推理正文；`tests/test_group_neighbors.py` 驗來源／預算／診斷；`scripts/probe_group_neighbors_mutations.py` 跑七項暫存變異。場景在`evals/group-memory-m4-suite.json`與`group-memory-m4-negative-suite.json`。[交付](handoff/GROUP-MEMORY-M4-DELIVERY.md)，不屬正式套件。

M5：`evals/prototypes/group_sequence.py` 只重排E的原文；`evals/prototypes/search_context.py` 包裝本機工具dispatch、補有界reply來源；`tests/test_group_sequence.py` 驗原文等價／實際搜尋接續／界線；`scripts/probe_group_sequence_mutations.py` 驗八項暫存變異。場景為`group-memory-m5-layout-suite.json`（E/F）、`group-memory-m5-search-suite.json`（A/G）。[M5交付](handoff/GROUP-MEMORY-M5-DELIVERY.md)。

M6：`evals/prototypes/search_attribution.py` 只核對實際發話帳號與reply作者，不推定內容主體；`tests/test_search_attribution.py` 驗身分／未知／額度／真實接續，`scripts/probe_search_attribution_mutations.py` 驗七項暫存變異。`evals/group-memory-m6-suite.json` 比G/H。[M6交付及最小正式接入方案](handoff/GROUP-MEMORY-M6-DELIVERY.md)，H整包未採入正式程式。

## 實驗中的查詢工具子代理

`evals/prototypes/rare_tool_delegate.py` 以選用模式接到真實executor；`scripts/rare_tool_live.py`做`:floor`的AB／BA比較。22項定向測試、20回合真實模型觀測。只實驗查詢，不改正式registry與預設；[結果與限制](handoff/RARE-TOOL-DELEGATE-DELIVERY.md)。

動作委派原型：`evals/prototypes/rare_action_delegate.py`；假的Discord寫入介面：`tests/harness_action_delegate.py`；自然語言條件轉參數與成功後不回呼主模型。正式預設未改；[成本與語意評估](handoff/RARE-ACTION-DELEGATE-DELIVERY.md)建議暫不接入。

工具子代理方向已依使用者決定放棄，只保留歷史原型。主模型教學已接入 `tools/registry.py`，由 `tests/test_tool_teaching.py` 驗證正式流程；[正式交付](handoff/TOOL-TEACHING-INTEGRATION-DELIVERY.md)。`evals/prototypes/tool_teaching.py` 與凍結的 `tool_teaching_baseline.py` 僅供歷史對照。

格式修復狀態：`conversation/repair.py` 與 `engine.py`。待查工具／參數、實際結果與已完成動作保護，由 `tests/test_tool_repair.py` 驗收；[完整交付](handoff/TOOL-REPAIR-DELIVERY.md)。

工具被拒後的回合定位：`conversation/repair.py`、`engine.py` 及 `tools/registry.py`。原訊息／可見 reply／實際送達的有界引用、移除未送草稿、同輪成功動作防重做、查詢前言之前的純參數校驗，由 `tests/test_tool_repair_focus.py` 驗證；[本批交付與語意反例](handoff/TOOL-REPAIR-FOCUS-DELIVERY.md)。角色卡錯誤說明單句候選在 `evals/prototypes/tool_focus_persona.py`，只由評估入口選用，正式 Bot 不匯入；`tests/test_tool_focus_persona.py` 核對僅一句不同及完整還原。

被拒時重組整份接續輸入的隔離原型：`evals/prototypes/rejected_input_reset.py`；只由實際 rejected 啟動，當前訊息置尾、保留可見工具證據／查證／更正與原權限，不另跑模型。`tests/test_rejected_input_reset.py` 與 `test_rejected_input_analysis.py` 核對資料及未完成分離；[實測與暫不接入的原因](handoff/REJECTED-INPUT-RESET-PROTOTYPE.md)。正式 Bot 沒有這個配置或匯入。

被拒後只解釋自身可見查詢字串的窄原型：`evals/prototypes/own_output_explanation.py`；實例包裝接續定位及查詢守衛，`tests/test_own_output_explanation.py`／`test_own_output_analysis.py` 驗來源、範圍與真實查證。`scripts/analyze_own_output_explanation.py` 分析付費證據，`replay_own_output_explanation.py` 離線重播；[主案例改善與後到改口缺口](handoff/OWN-OUTPUT-EXPLANATION-PROTOTYPE.md)。正式 Bot 沒有匯入，未接入角色卡或背景記憶政策。

後到明確原文查證研究原型：`evals/prototypes/late_verification.py` 在真實merge邊界擴充同一Verification物件；保留接入前Engine.run的code object、僅實例接線，不做全域class patch。歷史研究與變異測試透過`evals/prototypes/pre_continuation.py`核對封存雜湊後，使用舊run/generate_reply，避免正式接入後與原型雙重登記。`tests/test_late_verification.py`／`test_late_verification_analysis.py`、變異`scripts/check_late_verification_mutations.py`、分析`analyze_late_verification.py`；[歷史實讀與送達焦點](handoff/LATE-VERIFICATION-PROTOTYPE.md)。正式Bot未匯入原型；正式實作見頁首。

查完後的後到要求定位原型：`evals/prototypes/late_reply_focus.py` 在相同查證組裝後只替換一段已取得實際結果的system指令；查證前、其他模型角色與普通入口不改。`tests/test_late_reply_focus.py`／`test_late_reply_focus_analysis.py`、六組keyless worker對照 `scripts/check_late_reply_focus_offline.py`、四項變異 `check_late_reply_focus_mutations.py`、付費分析 `analyze_late_reply_focus.py`；[送達優先順序與保留反例](handoff/LATE-REPLY-FOCUS-PROTOTYPE.md)。正式Bot沒有匯入；若接入需整理正式狀態契約，不直接搬原型包裝。

