# 獨立安裝與套件交付

目前交付為4.6.0rc1，使用`dist/rc1-final/`最終包；直接安裝步驟及本機／未驗平台範圍見 [RC1指南](RC1_NOTES.md)。

4.6 使用自己的 `.venv`，不啟用或修改4.5的環境。`uv.lock` 鎖定依賴解析結果並保留Python／平台條件；它不是把本機 `pip freeze` 套到所有伺服器，也不代表所有平台都經過實測。環境實測範圍見 [O8交付](handoff/O8-DELIVERY.md)。

## 從原始碼安裝

以下使用已安裝的 uv；若使用系統 Python，先確認它符合 pyproject 的支援範圍。

```sh
uv sync --locked --extra math --no-dev
.venv/bin/python -B scripts/check_installation.py --math
.venv/bin/python -B scripts/warm_math_cache.py
cp config/runtime.example.toml config/runtime.toml
cp .env.example .env
.venv/bin/sayuki --config config/runtime.toml --check
```

核心聊天安裝可省略 `--extra math`；數學計算／繪圖／LaTeX圖像需要 math extra。`eval` extra 保留Pillow相容性，另含4.0比較工具需要的requests；4.6數學工具本身不用requests。開發與全套離線回歸另使用 `uv sync --locked --extra math --extra eval`（含dev檢查工具）。

`check_installation.py` 列出解譯器、套件版本與實際匯入位置；不載入憑證、不建立SDK client、不連Discord。`--check`只檢查配置與角色卡路徑，不等同所有套件／外部工具可用。首次數學執行可能建立字型快取，先預熱再驗收有界子程序。

未使用uv時仍可 `python -m venv .venv` 後 `pip install -e '.[math]'`，但該命令只依pyproject範圍解析，不會自動套用uv.lock。請自行記錄解析版本；不要把它稱為相同鎖定環境。

## 設定位置

- `--config` 優先於進程的 `SAYUKI_CONFIG`。
- 指定 `部署目錄/config/runtime.toml` 時，相對角色卡、資料庫與含目錄的Obscura路徑以部署目錄為根。
- 配置不在名為 `config` 的資料夾時，預設以配置所在目錄為根。
- 可用頂層 `project_root` 明確覆寫；相對值以配置所在目錄計算。
- 明確選擇配置時，`.env`只從該配置的最終root讀取；進程環境優先，不覆蓋已設定值。
- 原始碼模式未明確指定配置時，保留先讀專案 `.env`（可含SAYUKI_CONFIG）的相容流程。
- wheel安裝請明確給 `--config` 或進程環境中的SAYUKI_CONFIG；不要把資料或憑證放進site-packages。

外部配置的root預設已由原始碼ROOT改為上述規則；如果既有外部配置刻意引用開發checkout，請明確填 `project_root`。這是可攜性修正，不會搬移任何現有資料庫或角色卡。

## 建置與wheel安裝

```sh
uv build
python -m venv /path/to/runtime-venv
/path/to/runtime-venv/bin/python -m pip install 'dist/sayuki_bot-4.6.0rc1-py3-none-any.whl[math]'
/path/to/runtime-venv/bin/sayuki --config /path/to/deployment/config/runtime.toml --check
```

上述pip安裝wheel不會自動讀uv.lock。若需要重現鎖定版本，可先用`uv export --locked --extra math --no-dev --no-emit-project --format requirements-txt --output-file locked-requirements.txt`，在新環境以`pip install --require-hashes -r locked-requirements.txt`安裝依賴，再以`pip install --no-deps dist/sayuki_bot-4.6.0rc1-py3-none-any.whl`安裝程式。

wheel包含程式碼，不攜帶私人角色卡、憑證、SQLite或測試群組資料。部署目錄另外準備TOML、角色卡及新資料路徑。來源範例TOML的persona指向上層 `systemprompt.txt`，獨立搬移時務必調整；本程式不代建／改寫角色卡。

Obscura為外部執行檔，不是pip依賴：名稱 `obscura` 從PATH找，`bin/obscura`或`./obscura`以配置root解析，絕對路徑保留。套件安裝成功不表示此binary可執行或外網可達。本批不自動下載它或安裝systemd服務。

## 離線回歸

```sh
uv sync --locked --extra math --extra eval
MPLCONFIGDIR=/tmp/sayuki-tests .venv/bin/python -B scripts/warm_math_cache.py
PYTHONPATH=tests MPLCONFIGDIR=/tmp/sayuki-tests .venv/bin/python -B -m unittest discover -s tests -q
.venv/bin/ruff check --select F,E9 --target-version py311 src tests scripts
```

完整歷史比較測試需要工作區內的4.0來源、eval fixtures和封存快照；它們是測試資料依賴，不是4.6正式運行依賴。獨立wheel啟動檢查另以合成角色卡與暫存SQLite驗證，不把整個歷史比較套件是否隨wheel出貨混為一談。

## 正式啟動前

可先在尚未安裝 Bot 依賴的目標機執行 `python3 /path/to/check_installation.py --host-only`，取得標準庫主機盤點；不讀憑證、不控制服務。這不取代安裝後的依賴／外部工具檢查，亦不驗網路。模型各用途差異與真 Discord 的 D01–D14 表見 [部署前準備](DEPLOYMENT_PREFLIGHT.md)。

Python及平台宣告不等於各版本實測。伺服器目前由使用者估計為Debian13／Python3.12，仍待只讀核對版本、架構與服務啟動方式。尚未連SSH、部署、搬資料或替換運行中的4.0。
