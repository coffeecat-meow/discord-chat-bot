# Sayuki 4.6.0rc1

本包用於模擬驗收後的獨立試部署，尚未完成真實 Discord／正式主機驗收。

先閱讀 [試部署指南](docs/RC1_NOTES.md)，再依 [模型主備配置](docs/MODEL_ROUTES.md) 填入 chat、light、vision 各自的模型、endpoint及key名稱。角色卡與憑證另行提供，資料庫使用新路徑。

- 程式：`sayuki_bot-4.6.0rc1-py3-none-any.whl`
- 核心／數學依賴：`requirements-core.txt`／`requirements-math.txt`，取自同一份 `uv.lock`
- 配置：`config/runtime.example.toml`、`.env.example`
- 功能索引：[FEATURE_MAP](docs/FEATURE_MAP.md)
- 安裝：[INSTALLATION](docs/INSTALLATION.md)；操作與回退：[OPERATIONS](docs/OPERATIONS.md)
- 完成證據與限制：[RC-COMPLETION](docs/handoff/RC-COMPLETION.md)、`validation/`
- 每個檔案的SHA256列於 `MANIFEST.sha256`；該清單不包含自身。

wheel SHA256：`e3a68d3221bee962b33b5eac5faeb12ef85b3ade3498a5439eb8bfff275f52eb`

不包含私人角色卡、金鑰、SQLite、完整私有log、Obscura binary或全平台離線依賴wheelhouse。完整歷史回歸需原開發工作區及舊版fixtures，不屬部署依賴。不要使用中途的 `dist/rc1/` wheel替代本包。

Optional voice bridge and ASR setup: [docs/VOICE.md](docs/VOICE.md). Voice support is disabled by default.
