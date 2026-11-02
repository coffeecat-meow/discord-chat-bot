const path = require("path");
// 待切換：OpenCode Go 主、OpenRouter 備。與 ecosystem.config.js 同名，切換時先 delete 舊程序再 start，避免雙 Bot。
module.exports = {
  apps: [{
    name: "sayuki-4.6",
    cwd: __dirname,
    script: path.join(__dirname, ".venv/bin/sayuki"),
    args: "--config " + path.join(__dirname, "config/runtime.go-primary.toml"),
    interpreter: "none",
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    // 啟動時不從 CLI shell 繼承：Claude Code 開發／通訊變數，以及可能覆蓋 .env 的金鑰／配置變數（只列名稱，無值）。
    // 注意：PM2 只在全新 start 套用 filter_env；restart <ecosystem> 仍會合併 CLI 環境，日常請用 `pm2 restart sayuki-4.6`。
    filter_env: ["CLAUDE", "ANTHROPIC", "AI_AGENT", "COREPACK_ENABLE_AUTO_PIN", "GIT_EDITOR", "NoDefaultCurrentDirectoryInExePath",
                 "SAYUKI_CONFIG", "BOT_TOKEN", "OPENCODE_API_KEY", "opencode_api_key", "OPENROUTER_", "LLM_API_KEY"],
  }],
};
