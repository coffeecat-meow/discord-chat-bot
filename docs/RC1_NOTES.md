# 4.6.0rc1 交付與試部署指南

2026-10-17。此候選版的完成範圍是本機模擬驗收與可部署套件；真實 Discord、正式主機及服務切換另行驗收。完整證據與最新完成狀態見 [RC 主交付](handoff/RC-COMPLETION.md)。

## 這次完成的內容

- 大模型 `chat`、小模型 `light`、視覺模型 `vision` 各自設定主／備 endpoint、模型名稱及金鑰環境變數；`memory`、`attention`、`task` 可另外覆寫。範例為 OpenCode Go 優先、OpenRouter 備援，順序由配置決定。
- API／網路錯誤可在同一總期限內切到備援，記錄切換；取消、空白正文或本機程式錯誤不因此更換服務。已完成的工具與已送文字不重做。備援實際連線在本目標內禁止，已以真實 SDK 與假 HTTP 傳輸驗證。
- 背景整理按已確認方案啟用：low／8192，逐字來源片段、短確認來源關係、Bot 自身排除、積壓順序、取消與每日額度皆有覆驗。範本每40則／600秒，單批最多100則，每則1200字元，每日20次模型請求額度；有備援時先保留兩次額度，可能剩下一次而不再啟動新批次。
- schema 4 保存清除後不可靜默匯入的標記；只有本人明確同意才能恢復匯入。來源與長期記憶保留至刪除或撤回；普通診斷保留30天。新庫不自動搬入舊版資料。
- Bot 與匯入 CLI 共用實體資料庫身分鎖，涵蓋本機符號／硬連結別名。合成備份復原保留資料、清除阻擋與 unknown 動作回執。
- 普通聊天推理與角色卡不變；保留最後工具低推理、精簡工具教學、工具接續修復、睡眠／提醒、全群每人一次按鈕及明確任務入口。沒有恢復 OpenCC 或工具子代理。

功能所在位置見 [FEATURE_MAP](FEATURE_MAP.md)，配置說明見 [MODEL_ROUTES](MODEL_ROUTES.md) 與 [CONFIGURATION](CONFIGURATION.md)。

## 套件及安裝

使用 `dist/rc1-final/` 的最終 wheel 與交付 ZIP。`dist/rc1/` 是中途工程產物，不作本次交付。ZIP 不包含私人角色卡、憑證、SQLite、Discord 資料或平台 binary。

在新的部署目錄解開 deployment ZIP，確認 Python 至少3.11，然後執行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-core.txt
.venv/bin/python -m pip install --no-deps sayuki_bot-4.6.0rc1-py3-none-any.whl
cp .env.example .env
cp config/runtime.example.toml config/runtime.toml
```

若要使用數學工具，第一個 pip 安裝步驟改用 `requirements-math.txt`；兩份清單皆從同一份 `uv.lock` 匯出。依賴下載需要網路；交付包不是全平台離線 wheelhouse。

將既有角色卡另外複製到自己指定的位置，修改 `paths.persona`，不必改寫卡片。範例目前指向上層 `../systemprompt.txt`，它不隨套件出貨。填入管理員、信任伺服器、共享頻道，確認 `paths.database` 指向全新庫；`.env` 填 `BOT_TOKEN` 與所選路由的 key。每種用途可用不同模型與不同 key，不能把 key 寫進模型名稱或 TOML。未配置共享頻道不會自動共享。

```sh
.venv/bin/python -B scripts/check_installation.py
.venv/bin/sayuki --config config/runtime.toml --check
```

數學安裝另執行 `check_installation.py --math` 與 `warm_math_cache.py`。搜尋／讀網頁所需 Obscura 必須另外準備，`--check` 不驗外部服務可達。

以上檢查不啟動 Discord。真正開始測試前，先按 [DEPLOYMENT_PREFLIGHT](DEPLOYMENT_PREFLIGHT.md) 確認測試身分、頻道及可測操作；同一 Bot 身分不能由新舊兩版同時發送。實際啟動命令為：

```sh
.venv/bin/sayuki --config config/runtime.toml
```

本次沒有替使用者執行這個啟動步驟。備份與回退見 [OPERATIONS](OPERATIONS.md)；4.6 schema 4 不可交給4.0讀取，回退使用原4.0程式／配置／資料。

## 已知限制與未驗範圍

固定20個群聊情境各觀測三次。工程輸入、來源、排隊與送達契約通過，人物專項12次未見錯綁；仍有晚上行程與現在上午混淆、旁支重述，以及查完原文後擅自追問舊任務。自然確認意願與輕量日常 RP 仍允許。這是既有合成案例、非盲評，不能代表未知真群聊全面可靠。

Go 正式接話分類9次中7次有效且符合預期、2次在8秒期限逾時並安靜退出。320px紅藍圖片控制5/6正確，1次誤稱看不到；可送圖片不等於所有視覺情境都可靠。背景逐字契約覆驗18次完成且必要人物／更正控制通過，仍有一筆可選事件因證據欄位不符而拒收；拒寫優先於把無來源推測當事實。

乾淨安裝在本機 macOS／Python3.14.2 驗證。Python3.11語法相容檢查不等於Python3.12實跑。Debian／Linux、真實 Discord D01–D14、真實 OpenRouter 備援、跨日真群互動、舊資料實際遷移和正式切換均未驗。非 POSIX、跨主機鎖及外部替換資料庫檔案不在本機鎖保證內。

原始碼 ZIP 保存本次運行程式與操作文件；完整歷史比較測試仍依賴工作區中的舊版來源與封存 fixtures，不是獨立部署依賴。操作文件保留的歷史研究連結需回原工作區閱讀，部署包不附全部歷史報告。來源、測試、套件與帳本的對照以 RC 主交付及完整性報告為準。
