"""Standalone AI_Trade_Program (T212 + scanner sandbox). Not used by production dashboard."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_AITP_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _AITP_ROOT.parent
for p in (_AITP_ROOT, _REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env", override=False)
    load_dotenv(_AITP_ROOT / ".env", override=False)


_load_dotenv()

from flask import (  # noqa: E402
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("AI_Trade_Program")

app = Flask(
    __name__,
    template_folder=str(_AITP_ROOT / "templates"),
    static_folder=str(_AITP_ROOT / "static"),
)


@app.get("/")
def index():
    return render_template("ai_trade.html")


@app.get("/health")
def health():
    return jsonify(ok=True, service="AI_Trade_Program")


_exec_ns = {
    "__name__": "ai_trade_flask_routes",
    "app": app,
    "jsonify": jsonify,
    "request": request,
    "Response": Response,
    "stream_with_context": stream_with_context,
    "_log": _log,
    "logging": logging,
    "time": time,
    "threading": threading,
    "Any": Any,
    "json": __import__("json"),
}
code = (_AITP_ROOT / "ai_trade_flask_routes.py").read_text(encoding="utf-8")
exec(compile(code, str(_AITP_ROOT / "ai_trade_flask_routes.py"), "exec"), _exec_ns)


def main() -> None:
    try:
        from ai_sandbox import service as _ai_service

        _ai_service.start_in_background()
    except Exception:
        _log.exception("AI sandbox engine failed to start")
    host = (os.environ.get("AI_TRADE_PROGRAM_HOST") or "127.0.0.1").strip()
    port_raw = (os.environ.get("AI_TRADE_PROGRAM_PORT") or "5077").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 5077
    _log.info("AI_Trade_Program http://%s:%s/ (health /health)", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
