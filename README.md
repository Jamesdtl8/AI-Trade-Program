# AI_Trade_Program

Standalone Flask app for **AI Trade** and **Reasoning Test** UIs. Uses the **Trading 212 AI account** (`TRADING_212_*_AI`) and `ai_sandbox/` (Gemini/OpenAI, scanner feeds, SQLite journal).

Production **Discord + IBKR** trading lives in the sibling repo **`../Trading Platform/`** (`Main_Website/` + `Trading_AI/`) — not in this app.

The Discord relay still appends scanner JSONL under **`ai_sandbox/data/`** here (paths resolved as `../AI_Trade_Program/...` from the trading monorepo).

## Run

```bash
cd "/var/www/AI_Trade_Program"
# Optional: copy and edit env
# cp .env.example .env

python3 -m venv .venv-aitp
. .venv-aitp/bin/activate
pip install -r requirements.txt

python3 app.py
```

Defaults: `http://127.0.0.1:5077/` — override with `AI_TRADE_PROGRAM_HOST` / `AI_TRADE_PROGRAM_PORT`.

Health: `GET /health`

## Layout

| Path | Purpose |
|------|---------|
| `app.py` | Flask entry + `exec` of `ai_trade_flask_routes.py` |
| `ai_trade_flask_routes.py` | `/api/ai/*`, `/api/reasoning-test/*` |
| `templates/ai_trade.html` | Standalone dashboard page |
| `ai_sandbox/` | Engine, T212 client, DB, scanners |
| `trading_bot_t212_archive/` | Old `t212.py` reference copies |
| `docs/` | Notes moved from repo root |

See `docs/AI Trade.md` for behaviour detail.

## Maintenance

Regenerate `templates/ai_trade.html` from the main dashboard (run from this tree; requires `../Trading Platform` to be a git checkout):

```bash
python3 scripts/_build_ai_trade_template.py
```
