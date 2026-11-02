# 運行與回退

請先依[獨立安裝](INSTALLATION.md)建立4.6自己的環境；核心包含Pillow，已移除OpenCC自動轉換，數學工具另需math extra。Discord CDN 必須可達；Lottie 只嘗試靜態預覽，失敗不自動安裝渲染器或轉用第三方服務。

先跑本機 `--check` 與離線測試，再在指定測試群使用獨立Bot身分驗證。此程式不會自行採用舊版憑證或啟動來源Bot。

一般 `/sayuki` 管理指令只接受配置的管理員且位於信任伺服器。本人恢復舊資料同意的 `data_restore_import` 是例外，僅能確認自己，見 [資料政策](DATA.md)。status、interrupt、look、reload_prompt、reminders、cancel_reminder、memory_user、memory_permanent、memory_server、clear_status、permissions、debug_last、logs、tool_stats、user_stats已註冊。用量與trace僅展示目前伺服器可對應的回合，不回傳私人角色卡。

目前 RC 的睡眠提醒暫存至清醒後再送，按鈕不叫醒，管理狀態與interrupt可用。使用者已確認睡眠時延到清醒後；提醒已經由同一聊天模型自然轉達，並共用頻道佇列；生成失敗時保留明確提醒內容作為備援。不要先遷移正式提醒。

Bot啟動持有 DatabaseLock：資料庫側檔與device/inode的獨立身分鎖，能辨識同一檔案的符號／硬連結。同一Bot身分即使使用不同資料庫也不要啟兩個版本；鎖並不跨主機協調。不要刪除持有中的lock檔，見 [資料政策](DATA.md)。

發送或動作結果不明時查receipts，不盲目重播。進程重啟時pending回執改成unknown；待回合佇列不會自動重放。按鈕View有10分鐘期限，重啟後不持久恢復。

回退：停止4.6 → 保存其資料副本 → 使用原4.0程式／配置／資料重新啟動。不要讓4.0讀4.6 schema，也不直接把4.6資料覆蓋舊檔。正式切換前須額外核對待送提醒與其他未決動作。

早期測試借用4.5環境；O8已另建4.6環境與uv.lock，實測版本及限制見[O8交付](handoff/O8-DELIVERY.md)。伺服器版本仍待核對。

## 離線匯入

先停止來源與目的 Bot，保留可回退備份，再使用 `scripts/import_memory.py` 的預設 dry-run。來源與目的不可同檔（含硬連結）；apply 使用與Bot相同的DatabaseLock，涵蓋相同實體檔案的路徑別名；它仍不能取代正式停機流程。

目的 schema 1／2／3 只在私有副本升至4，dry-run 不改原檔或旁檔；apply 寫回升級結果，同時包含正式 Database 啟動維護行為。未知 schema 拒絕。讀取 WAL 來源／目的時會複製主檔和 WAL 到暫存目錄，需要對應磁碟空間；來源必須停止寫入以避免非同步副本。

失敗時查看 stderr JSON 的 `destination_written` 與 `partial_report`，不要僅憑非零退出推定沒寫入。合成測試已確認匯入前／寫回前的失敗與鎖清理；寫回磁碟故障、真實資料及所有平台尚未驗收。見 [O7-A 複核](handoff/O7-A-CODEX-REVIEW.md)。

## 明確任務入口（O6-E）

信任群組內可使用 `/task start prompt:...`，結果在目前頻道送出；私人確認訊息附任務 ID。`/task cancel task_id:...` 可取消自己在本頻道的任務，配置的 Bot 管理員可取消他人的。睡眠中 start 拒絕入列，cancel 可用。普通聊天不自動進入此入口。

新配置以 `model.routes.task` 覆寫任務主／備路由，未設沿用chat，見 [模型路由](MODEL_ROUTES.md)。舊無routes配置的 `model.task` 留白沿用 `model.chat`；可設定 `model.task_max_output_tokens` 與 `model.task_reasoning_effort`，未設定沿用聊天值。模型服務與金鑰沿用現有 ModelClient；不增加前置分類或渲染模型。角色卡不變。任務使用既有工具及權限，並不取得額外管理權。

`tasks.max_tool_rounds=6` 允許最多六輪工具接續、七次主模型生成；每輪工具數仍由 `conversation.max_tools_per_round` 控制。視覺等工具內部模型呼叫及 SDK 重試另計，七次不代表全部網路請求上限。`tasks.timeout_seconds=180` 從出列開始計時，涵蓋上下文準備、模型、工具與發送；排隊不計入。這是 asyncio 的合作式取消，不是程序隔離的硬期限；阻塞事件迴圈或忽略取消的依賴仍可能延後收尾。

同頻道繼續依序處理，長任務會讓該頻道後續聊天排隊；其他頻道獨立。任務可取消、有限時，但本批不承諾長任務期間同頻道的首段延遲。重啟不恢復任務。時間到或取消會停止後續工作，不撤回已完成操作；發送在途被取消仍記 unknown，不自動重送。任務失敗／超時會嘗試用原互動的私人回覆通知；若互動 token 已失效，會留下日誌，不能保證 Discord 已顯示通知。

2026-09-29 使用者已確認睡眠提醒延到清醒後。除了到期掃描，提醒出列時也再查一次作息；尚未開始送出的睡眠回合回到 pending。已開始的提醒不因跨過睡眠邊界而拆斷。保留配置 `reminders.during_sleep` 的既有相容性，但部署採 false。

## 社交工具的操作前檢查（O6-E2）

原生 `POLL` 的 minutes 需為60的倍數，範圍60–10080；不支援的分鐘數直接回報錯誤，不會自動延長。`INSPECT_POLL` 帶 finalized；尚未定案時正數是暫時觀測，0因SDK可能只是預設值而回傳null，不能把null讀成零票。定案後才確定零票。沒有讀取投票者名單。

活動日期、反應目標與輔助文字在取得動作回執之前檢查。討論串／私訊的非法文字不會先建立外部物件；已開始的操作如果逾時、取消或收到平台錯誤仍採保守unknown，不重送。已取消回合不再開始新動作。群友自主狀態與有界禁言、暱稱／活動原有權限規則維持。


## RC 本機備份／復原步驟

先停止該資料庫的寫入程序，以SQLite backup API建立單一一致快照；不要只複製運行中WAL庫的主檔，也不要覆蓋原資料或升級前備份。例如，在確認Bot已停止後（目標backup檔必須不存在）：

```python
from contextlib import closing
from pathlib import Path
import sqlite3

source = Path("/path/to/stopped/data/sayuki.sqlite3")
backup = Path("/path/to/new-backup.sqlite3")
if not source.is_file() or backup.exists():
    raise RuntimeError("source missing or backup already exists")
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

SQLite可能在來源旁建立WAL／SHM側檔；不需要無副作用來源目錄時，這不代表內容被改。來源必須完全唯讀且不可產生側檔時，使用匯入器既有的 `read_only_source` 暫存副本機制；仍須先停止來源寫入。

復原到另一個全新路徑，先核對schema、integrity_check、來源／畫像、privacy／import_erasure、收據與提醒狀態；不要重新排送unknown。配置指向復原副本後先做 `--check`。4.6 schema4的副本不能交給舊程式；回退至4.0時只使用原先保留的4.0資料。這次合成演練已核對邏輯資料一致、清除阻擋與unknown防重做，未操作真主機、舊庫或服務。
