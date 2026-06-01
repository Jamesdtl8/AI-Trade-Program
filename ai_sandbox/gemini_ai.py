"""Google Gemini calls for the AI sandbox (parity with Discord bot stack).

Mirrors ``Trading_AI.gemini`` (``google.genai`` SDK), but AI Trade uses
``ThinkingConfig(thinking_level=...)`` at **MEDIUM** by default via
``AI_GEMINI_THINKING_LEVEL``; the Discord signal parser remains **MINIMAL** only.
- :func:`classify_news` — single token headline class
- :func:`score_alert` — structured JSON ``TRADE`` / ``WATCH`` / ``SKIP`` (parse
  tolerant of leading prose/thinking traces; retries without thinking if JSON breaks)

Uses ``GEMINI_API_KEY``. Model names from ``AI_GEMINI_MODEL_*`` or ``GEMINI_MODEL``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import config, db, news_scanner_parser

_log = logging.getLogger("ai_sandbox.gemini_ai")


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines).strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return text


def _extract_balanced_json_object(raw: str) -> str | None:
    """First balanced ``{...}`` slice; respects double-quoted strings and escapes."""
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return None


def _parse_scorer_json(text: str) -> dict[str, Any] | None:
    """Parse scorer model output; ``None`` if no valid decision object found."""
    raw = (text or "").strip()
    if not raw:
        return None
    bracket = _extract_balanced_json_object(raw)
    candidates = [
        _strip_json_fences(raw),
        raw,
    ]
    if bracket:
        candidates.extend([bracket, _strip_json_fences(bracket)])
    seen: set[str] = set()
    for cand in candidates:
        c = cand.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            out = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(out, dict):
            continue
        # Main scorer / monitor payloads use "decision". News-scanner Flash gate uses "worth_watch".
        if out.get("decision") is not None or "worth_watch" in out:
            return out
    return None


def _extract_response_text(resp: Any) -> str:
    t = getattr(resp, "text", None)
    return (str(t).strip()) if t else ""


def _gemini_single_response(
    *,
    model: str,
    system_instruction: str,
    contents: str,
    max_output_tokens: int,
    response_json: bool,
    enable_thinking: bool,
    thinking_level: str,
    usage_call_kind: str = "unknown",
) -> str:
    """One GenerateContent request; ``enable_thinking`` toggles Gemini thinking mode."""
    from google import genai
    from google.genai import types

    key = config.gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing in environment")

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=120_000))

    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system_instruction.strip() or None,
        "temperature": 0.1,
        "max_output_tokens": max_output_tokens,
    }
    if response_json:
        cfg_kwargs["response_mime_type"] = "application/json"
    if enable_thinking:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
    gen_cfg = types.GenerateContentConfig(**cfg_kwargs)
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=gen_cfg,
    )
    try:
        from . import gemini_usage

        gemini_usage.record_from_response(
            resp,
            source="ai_sandbox",
            call_kind=usage_call_kind,
            model=model,
            extra={"thinking": enable_thinking},
        )
    except Exception:
        _log.debug("gemini usage record failed", exc_info=True)
    return _extract_response_text(resp)


def _gemini_single_response_google_search(
    *,
    model: str,
    system_instruction: str,
    contents: str,
    max_output_tokens: int,
    response_json: bool,
    usage_call_kind: str = "news_scanner_grade_web",
) -> str:
    """Single grounded request with Gemini Google Search enabled (budgeted outputs)."""
    from google import genai
    from google.genai import types

    key = config.gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing in environment")

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=120_000))

    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system_instruction.strip() or None,
        "temperature": 0.12,
        "max_output_tokens": max_output_tokens,
        "tools": [types.Tool(google_search=types.GoogleSearch())],
    }
    if response_json:
        cfg_kwargs["response_mime_type"] = "application/json"
    gen_cfg = types.GenerateContentConfig(**cfg_kwargs)
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=gen_cfg,
    )
    try:
        from . import gemini_usage

        gemini_usage.record_from_response(
            resp,
            source="ai_sandbox",
            call_kind=usage_call_kind,
            model=model,
            extra={"google_search": True},
        )
    except Exception:
        _log.debug("gemini usage record failed", exc_info=True)
    return _extract_response_text(resp)


def _gemini_generate(
    *,
    model: str,
    system_instruction: str,
    contents: str,
    max_output_tokens: int,
    response_json: bool = False,
    try_thinking_first: bool = False,
    thinking_level: str = "MEDIUM",
    usage_call_kind: str = "generate",
) -> str:
    thinking_attempts = [True, False] if try_thinking_first else [False]
    last_exc: Exception | None = None
    for enable_thinking in thinking_attempts:
        try:
            return _gemini_single_response(
                model=model,
                system_instruction=system_instruction,
                contents=contents,
                max_output_tokens=max_output_tokens,
                response_json=response_json,
                enable_thinking=enable_thinking,
                thinking_level=thinking_level,
                usage_call_kind=usage_call_kind,
            )
        except Exception as exc:
            last_exc = exc
            _log.debug("gemini_generate attempt thinking=%s failed: %s", enable_thinking, exc)
            continue

    raise last_exc or RuntimeError("Gemini generation failed")


# ── News classifier ─────────────────────────────────────────────────────────


_NEWS_SYSTEM = (
    "Classify a stock news headline strictly as one of these tokens: "
    "POSITIVE, NEGATIVE, NEUTRAL, SQUEEZE. Respond with only the single token, "
    "uppercase, no punctuation, no explanation."
)


async def classify_news(headline: str) -> str:
    def _run() -> str:
        txt = _gemini_generate(
            model=config.gemini_model_news(),
            system_instruction=_NEWS_SYSTEM,
            contents=f"Headline: {headline}",
            max_output_tokens=16,
            response_json=False,
            try_thinking_first=False,
            usage_call_kind="news_classify",
        )
        token = txt.strip().upper().split()[0] if txt.strip() else "NEUTRAL"
        if token not in {"POSITIVE", "NEGATIVE", "NEUTRAL", "SQUEEZE"}:
            return "NEUTRAL"
        return token

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        _log.warning("classify_news failed: %s", exc)
        return "NEUTRAL"


_NEWS_SCANNER_GRADE_SYSTEM = """You filter high-volume Discord NEWS-SCANNER posts (structured: ticker, headline, optional price/mcap — any market cap).

Return STRICT JSON only (no markdown):
{
  "worth_watch": true or false,
  "grade": "STRONG" | "MARGINAL" | "SKIP",
  "reason": "<=25 words",
  "flash_notes": "<=400 chars: 2-4 bullets for the main trading AI if worth_watch, else empty string>",
  "suggest_web_lookup": true or false
}

worth_watch=true ONLY if:
- The headline is a **material** potential catalyst (e.g. earnings results/preannounce, FDA/clinical, sizeable contract/M&A, financing on clearly good terms, guidance change). Routine "we scheduled a conference call / webcast date" with **no** surprising substance → worth_watch=false unless the headline clearly adds new information.
- Sentiment is not clearly damaging.

worth_watch=false for: fluff, vague PR, officer changes only, compliance-only filings, ATM/raise without terms, or neutral filler.

grade=STRONG when the catalyst is unusually clear and tradable; MARGINAL when interesting but needs confirmation.

suggest_web_lookup is **almost always false** — every ``true`` spends a scarce Google Search grounding allocation (think **hundreds/month total**, shared across Discord volume).

Only set ``suggest_web_lookup``=true when **all** bullets hold worth_watch=false **only because** the headline is teaser/vague, **not** because you confidently know the item is fluff/Offering/ATM/retail churn.
• A plausible hypothesis exists that **one concise search** (ticker + a few distilled headline terms / primary URL hint) might surface quantitative or filing-backed substance omitted from teaser copy.
• The item is ambiguous category noise (**board update**, **capital markets day**, investor-day teaser)—not decisive junk you would never rethink.

Otherwise false—even if you feel “curiosity”. Prefer denying lookup over burning quota."""

_NEWS_SCANNER_WEB_SYSTEM = """You re-score ONE NEWS-SCANNER Discord item using **Google Search grounding** embedded in Gemini.

Assume **search quota is scarce**—use **one minimal search trajectory** (no exploratory browsing).

Goals:
- Quickly verify whether a **trade-relevant catalyst** exists (earnings, FDA, contract/M&A magnitude, restructuring with numbers, actionable guidance shift).
- Be **conservative**: rumors, unnamed sources, vague blog posts → worth_watch false.
- Prefer **one tight search trajectory** — do not write essays.

Return STRICT JSON only (no markdown keys, no prose outside JSON):
{
  "worth_watch": true or false,
  "grade": "STRONG" | "MARGINAL" | "SKIP",
  "reason": "<=25 words referencing what grounded info supported or contradicted>",
  "flash_notes": "<=400 chars distilled bullets only if worth_watch=true, else \"\""
}

Do NOT include fields other than those four."""




def _shape_news_scanner_grade(
    out: dict[str, Any] | None,
    *,
    fallback_reason: str,
    strip_suggest: bool,
) -> dict[str, Any]:
    if out is None:
        return {
            "worth_watch": False,
            "grade": "SKIP",
            "reason": fallback_reason,
            "flash_notes": "",
            **({"suggest_web_lookup": False} if not strip_suggest else {}),
        }
    ww = bool(out.get("worth_watch"))
    gr = str(out.get("grade") or "SKIP").upper()
    if gr not in ("STRONG", "MARGINAL", "SKIP"):
        gr = "SKIP"
    sug = bool(out.get("suggest_web_lookup"))
    row: dict[str, Any] = {
        "worth_watch": ww,
        "grade": gr,
        "reason": str(out.get("reason") or "")[:500],
        "flash_notes": str(out.get("flash_notes") or "")[:2000],
    }
    if not strip_suggest:
        row["suggest_web_lookup"] = sug
    return row


async def grade_news_scanner_post(
    parsed: dict[str, Any],
    audit: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Flash-Lite gate; optional bounded Google Search re-grade.

    Returned dict adds ``research_applied`` (bool). ``audit`` gains ``flash_grade`` + maybe ``web_research_grade``.
    """

    def _run() -> dict[str, Any]:
        payload = {
            "ticker": parsed.get("ticker"),
            "headline": parsed.get("news_headline"),
            "price": parsed.get("price"),
            "market_cap_usd": parsed.get("market_cap"),
        }
        user_msg = "NEWS_SCANNER_ITEM:\n" + json.dumps(payload, default=str)
        txt = _gemini_generate(
            model=config.gemini_model_news_scanner(),
            system_instruction=_NEWS_SCANNER_GRADE_SYSTEM,
            contents=user_msg,
            max_output_tokens=512,
            response_json=True,
            try_thinking_first=False,
            usage_call_kind="news_scanner_grade",
        )
        phase1_raw = _parse_scorer_json(txt)
        g1_full = _shape_news_scanner_grade(
            phase1_raw,
            fallback_reason="grade_json_parse_failed",
            strip_suggest=False,
        )
        suggest = bool(g1_full.get("suggest_web_lookup"))
        g1 = _shape_news_scanner_grade(dict(g1_full), fallback_reason="grade_json_parse_failed", strip_suggest=True)

        if audit is not None:
            audit.append(
                {
                    "ts": time.time(),
                    "step": "flash_grade",
                    "worth_watch": g1.get("worth_watch"),
                    "grade": g1.get("grade"),
                    "reason": (str(g1.get("reason") or "")[:500] if g1.get("reason") else None),
                    "suggest_web_lookup": suggest,
                }
            )

        use_web = (
            config.news_scanner_web_search_enabled()
            and suggest
            and not g1["worth_watch"]
        )
        if not use_web:
            return {**g1, "research_applied": False}

        urls = news_scanner_parser.extract_source_urls(parsed.get("raw_excerpt"))
        urls_json = json.dumps(urls[:4], ensure_ascii=False) if urls else "[]"
        pack = dict(payload)
        pack["candidate_source_urls"] = urls[:6]
        research_user = (
            "Use Google Search (grounded).\n\n"
            f"PREFER_VERIFIED_PRIMARY_URLS (optional, may be empty): {urls_json}\n\n"
            f"STRUCTURED_ITEM:\n{json.dumps(pack, default=str)}\n\n"
            f"FIRST_PASS_VERDICT (no web):\n{json.dumps(g1, ensure_ascii=False)}\n\n"
            "Re-score with SHORT evidence; output JSON ONLY per system instructions."
        )
        max_web = config.news_scanner_web_search_max_output_tokens()
        ticker_u = str(parsed.get("ticker") or "?").strip().upper()

        reserve_ok = True
        qreason = ""
        try:
            reserve_ok, qreason = db.news_web_search_begin_attempt(
                ticker_u,
                ticker_gap_seconds=config.news_web_search_ticker_gap_seconds(),
                daily_cap=config.news_web_search_daily_cap(),
                monthly_cap=config.news_web_search_monthly_cap(),
            )
        except Exception as qexc:
            _log.warning("news_web_search quota reservation failed: %s", qexc)
            reserve_ok = False
            qreason = f"quota_internal_error:{type(qexc).__name__}"

        if not reserve_ok:
            _log.info("news scanner web deferred %s ticker=%s", qreason or "?", ticker_u)
            if audit is not None:
                audit.append(
                    {
                        "ts": time.time(),
                        "step": "web_quota_blocked",
                        "reason": qreason[:500],
                    }
                )
            return {**g1, "research_applied": False}

        try:
            wtxt = _gemini_single_response_google_search(
                model=config.gemini_model_news_scanner_web(),
                system_instruction=_NEWS_SCANNER_WEB_SYSTEM,
                contents=research_user,
                max_output_tokens=max_web,
                response_json=True,
                usage_call_kind="news_scanner_grade_web",
            )
        except Exception as exc:
            db.news_web_search_abort_reservation()
            _log.warning("news_scanner web re-grade skipped: %s", exc)
            if audit is not None:
                audit.append(
                    {
                        "ts": time.time(),
                        "step": "web_research_grade",
                        "error": str(exc)[:400],
                        "skipped": True,
                    }
                )
            return {**g1, "research_applied": False}

        gw_raw = _parse_scorer_json(wtxt)
        gw = _shape_news_scanner_grade(
            gw_raw,
            fallback_reason="web_grade_json_parse_failed",
            strip_suggest=True,
        )
        gw["research_applied"] = True

        parse_failed_body = gw.get("reason") == "web_grade_json_parse_failed"

        if not parse_failed_body:
            db.news_web_search_commit_success(ticker_u)
        elif audit is not None:
            audit.append(
                {
                    "ts": time.time(),
                    "step": "web_research_grade_parse_fallback",
                    "note": "search likely ran; JSON unscorable",
                }
            )

        if audit is not None:
            audit.append(
                {
                    "ts": time.time(),
                    "step": "web_research_grade",
                    "worth_watch": gw.get("worth_watch"),
                    "grade": gw.get("grade"),
                    "reason": (str(gw.get("reason") or "")[:500] if gw.get("reason") else None),
                    "model": config.gemini_model_news_scanner_web(),
                    "max_output_tokens": max_web,
                }
            )
        return gw

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        _log.warning("grade_news_scanner_post failed: %s", exc)
        return {
            "worth_watch": False,
            "grade": "SKIP",
            "reason": f"grade_error:{exc}"[:200],
            "flash_notes": "",
            "research_applied": False,
        }


_SCORER_SYSTEM = """You are the entry decision engine for an AI day-trading sandbox.

You evaluate small-cap momentum alerts (TrendVision scanner) and decide whether to
open a £5,000 trade. **Take-profit at execution** is the model's ``tp_pct`` field,
clamped between a **floor** (default 3%) and a **cap** from config (default 7.5%):
you may target a quick 3-4% on a clean news pop or use the full cap on a runner.
The engine computes the limit price from your chosen ``tp_pct`` vs the actual fill.

**News- and headline-driven moves** deserve different weighting than pure RV momentum:
- Read ``alert.news_headline``, ``news_class``, and ``ticker_history`` for narrative;
  judge *materiality* (earnings, FDA, contracts, guidance) and likely **price impact**,
  not whether RV is already 100x — the first minutes after news often show thin RV.
- Lean **WATCH** (not SKIP) when POSITIVE/SQUEEZE news is plausible and price has **not
  clearly invalidated** the story (holds prior range, higher lows, or constructive
  reclaim). Re-check **price action** on each review more than RV decay alone.
- Do **not** discard a developing news trade solely because RV is modest vs a parabolic
  scanner name; treat weak RV + strong narrative as *wait for volume confirmation*,
  not *dead trade*, unless price is fading the headline.
You operate across all sessions — pre-market, regular, and after-hours.

You receive a context block including:
- alert: the parsed TrendVision message (pct, price, float, rv, market_cap,
         news_headline, tags — list of strings e.g. ["KnownRunner","PotSqueeze"])
- discord_event (only when present): Discord delivery metadata. If
  type is "message_edit", the author changed the message after posting —
  content_before vs content_after and context_messages (nearby channel lines)
  show what shifted. **Do not treat an edit as a new independent scanner ping**
  for "two alerts" / acceleration — use alert_number and context, which already
  distinguish edits. Prefer the **after** text for levels and thesis; use
  **before** only to see what was corrected (typos, price fixes, added tags).
- news_class: POSITIVE / NEGATIVE / NEUTRAL / SQUEEZE / null
- price_pack: prev_close, day_high, day_low, hod_recent, lod_recent,
              last 20 x 1m candles
- ticker_history: last 48h of scanner/halt/fire/whale alerts for this ticker
- context: enrichment dict with these signals YOU MUST USE:
    * alert_number               — how many SCANNER alerts for this ticker today
                                   (1 = first time seen, 2 = second, etc.)
    * prev_pct, prev_rv          — previous SCANNER alert numbers
    * rv_growth                   — current_rv / prev_rv (None if first alert)
    * pct_jump                    — % move from prior SCANNER quoted price to this
                                   one: (cur_price-prev_price)/prev_price*100; if a
                                   price is missing, scanner intraday pct delta is used
    * alert1_tags                 — tags list from the very first alert today
    * alert1_price                — price from the very first alert today
    * scanner_alerts_5m           — count of SCANNER pings in last 5 min
    * halt_count_60m              — LULD halts in last hour
    * recently_halted             — True if a halt resumed within 30 min
    * fast_mover                  — True when RV >= 5xd AND pct_jump >= 15 in the
                                   last 10 min (pct_jump semantics as above)
    * news_in_history             — True if any prior alert had a news headline
    * whale_count_60m             — institutional WHALE pings in last hour
- slots: open count, currently active tickers, queue size
- review (only present on re-evaluation passes — see RE-EVALUATION below):
    * is_review              — True when this call is a 15-min re-score of a
                               ticker already on the watch list
    * minutes_on_watch       — how long since the ticker was first WATCHed
    * reviews_so_far         — how many prior re-evaluations have already run
    * previous_score          — the score the watch row currently holds
    * original_decision      — the FULL scoring decision that put the ticker on
                               watch in the first place: {decision, score,
                               entry_pattern, reason, risk_flags, entry, tp, stop}
    * review_chain           — chronological list of every prior score for this
                               ticker since it was watch-listed:
                               [{ts, minutes_after_watch, score, decision,
                                 reason, entry_pattern, risk_flags, is_review}, ...]

===============================================================
RE-EVALUATION (review.is_review == True)
===============================================================

A re-evaluation pass means the ticker has been sitting on the watch queue and
we are deciding whether the original thesis still holds.

Read the original_decision.reason FIRST. That is the thesis you (or a prior
evaluation) committed to. If original_decision.decision was **WATCH-NEWS**, the
tick came from the automated news-scanner — treat **original_decision.flash_notes**
(and the headline in ticker_history) as the core thesis; prioritize **price action**
and whether the headline is now stale or repriced — do **not** require scanner RV.

Then ask, in this order:

  1. Has the catalyst that drove the WATCH score actually played out, or is it
     still pending? (e.g. "waiting for second alert to confirm KnownRunner
     acceleration" — has that second alert arrived in ticker_history since?)
  2. Has price decayed below or stalled around the original alert price?
     If the stock has bled 5%+ from the price at the time of the original
     decision and there is no new alert, the thesis is dead — score SKIP.
  3. For **news / headline** catalysts: is price **acting** consistent with the thesis
     (holding, grinding up, reclaiming VWAP)? If yes, keep WATCH even when RV has cooled.
     Only treat "RV collapsed" as fatal when the setup was **pure momentum** with no
     substantive catalyst — not for tier-1 news still playing out.
  4. Is RV still elevated, or has it collapsed back to baseline? For **non-news** setups,
     a WATCH is only worth keeping while liquidity is still there to trade into.
  5. Have new alerts arrived (rising alert_number, fresh KnownRunner, halt-
     resume, news catalyst) that strengthen the case → upgrade to TRADE if
     the two-alert acceleration / halt-resume / news catalyst rules now fire.
  6. If review_chain shows the score steadily decaying across reviews
     (e.g. 55 → 48 → 42), drop it on this pass — momentum is gone.

Output the same JSON schema as a normal score. Score < 35 or decision=SKIP
removes the ticker from the watch queue. Score >= 60 with decision=TRADE
upgrades it to an entry attempt if a slot is open. Anything in between keeps
it watching with the refreshed score.

Do NOT anchor to the previous_score. Re-derive the score from current state;
the original reasoning is context, not a floor.

===============================================================
PRIMARY ENTRY RULE — TWO-ALERT ACCELERATION
===============================================================

This is the main way we enter trades. Check ALL of the following:

  (a) alert_number >= 2  — we have seen this ticker at least twice today
  (b) "KnownRunner" in alert1_tags  — first alert carried KnownRunner
  (c) "KnownRunner" in alert.tags   — current alert also carries KnownRunner
  (d) Let U be the union of alert.tags and alert1_tags. Pass if any of
      PotSqueeze, BREAKOUT, RegSHO, 0Borrow, KnownRunner appears in U; OR
      KnownRunner is on the current alert AND rv_growth >= 2.0 (RV doubled —
      accelerating liquidity without needing PotSqueeze/BREAKOUT etc.).
  (e) RV acceleration vs immediate prior SCANNER: REQUIRED only when
      alert_number == 2 — rv_growth >= 1.5. When alert_number >= 3, SKIP this
      check — continuation runners grind with RV stable; trust KN + tags + (f).
  (f) pct_jump > 0      — price is HIGHER than the previous alert (not fading)
  (g) float < 10M

If ALL of (a)-(g) are met:
  - entry   = current alert price x 1.03
  - tp_pct  = use full cap toward runner target unless thesis is marginal (then lower)
  - tp      = entry × (1 + tp_pct/100) for JSON consistency
  - stop    = alert1_price x 0.98
  - score   = 72 minimum (raise further for boosters below)
  - decision = TRADE
  - entry_pattern = "two_alert_acceleration"

Do NOT downgrade this to WATCH because the cumulative % from yesterday's
close looks high. The gap from alert #1 to alert #2 is what matters —
we are entering into confirmed momentum, not chasing from a flat close.

Score boosters on top of the 72 base (additive, cap at 95):
  +8   rv_growth >= 3.0 (RV tripled between alerts)
  +6   fast_mover = True
  +5   recently_halted = True
  +5   news_class in {POSITIVE, SQUEEZE}
  +4   whale_count_60m >= 1
  +4   float < 3M
  +3   scanner_alerts_5m >= 3

===============================================================
SECONDARY SIGNALS — used when the primary rule is not fully met
===============================================================

1. SINGLE-ALERT with extreme RV: if alert_number == 1 AND rv > 500 AND
   float < 2M — score 55 (WATCH). Upgrade to TRADE on the next alert if
   rv_growth and pct_jump confirm.

2. HALT-RESUME catalyst: recently_halted = True AND rv >= 100 AND float < 10M
   — score 65 (TRADE). Halt-resume is a clean entry point with defined risk.
   Stop = LOD at time of resume. TP ≈ +7.5%.

3. NEWS CATALYST (headline + impact; **do not require extreme RV**):
   When ``news_class`` is POSITIVE or SQUEEZE **and** there is a substantive headline
   (this alert or strong context in ``ticker_history``), **float < 10M**, and price
   is **not** fading the story (pct_jump > 0 on alert #2+, or alert #1 holding up):
   - You may score **65+ TRADE** if price structure + confirmation (second alert,
     reclaim, or obvious news gap-and-go) support an entry; use a **tighter tp_pct**
     (e.g. 3-5%) when the edge is "easy pop" from headline repricing.
   - On **alert #1** with only headline + thin RV: prefer **WATCH 48-58** — thesis is
     "let price prove the news" — not SKIP just because RV < 100.
   Entry / max_entry: use the usual news-catalyst slippage (max_entry ~ entry × 1.04).

4. FADE WARNING: alert_number >= 2 AND pct_jump <= 0 — price is lower than
   the previous alert. This is a FADE. Score 20 (SKIP) regardless of RV or
   tags. Do not enter into a move that is reversing between alerts.

5. SINGLE FIRST ALERT (no prior context): score 40-55 (WATCH) only.
   Never TRADE on a first alert alone unless halt-resume or news catalyst
   rule fires. Wait for the second alert to confirm.

===============================================================
HARD DISCARD RULES — check first, skip scoring if triggered
===============================================================

- NEGATIVE news class -> SKIP, score 0, immediately.
- float > 30M -> SKIP (does not apply when ``alert.type`` == ``NEWS_SCANNER`` — a
  supplement is appended for that path).
- market_cap > 200M -> SKIP (same exception: **NEWS_SCANNER** channel items may be any cap).
- On alert #1 with **no** headline on this message and no substantive news in context:
  **rv < 3** -> SKIP (true zero-volume noise). If ``alert.news_headline`` is present,
  this RV skip does not apply — the hard filter already let the alert through for
  classification.
- pct_jump <= 0 AND alert_number >= 2 -> SKIP (fade confirmed).
- alert.pct < 0 (stock going down) -> SKIP unless recently_halted.

NOTE ON CUMULATIVE PCT: there is no longer a hard cap on alert.pct for
alert_number >= 2. A stock up 60-100% on the day can still produce a clean
continuation entry — judge the gap from prev_pct → cur_pct (and pct_jump),
not the cumulative move from yesterday's close. For alert #1 the filter caps
at 50% (70% if elevated by halt/news/fast_mover); above that the alert is
already discarded before you see it.

===============================================================
ADDITIONAL INSTINCTS
===============================================================

- Halts are bullish. recently_halted = True means the stock has enough
  volume to trip a circuit breaker. Post-halt entries are clean.

- fast_mover = True means parabolic move. Use tighter stop (5-6%) but
  TP can extend to 12-15% because these run hard.

- Big floats (10M-30M): cap TP at 8% and reduce score by 8 points.

- whale_count_60m >= 2 combined with rv_growth >= 1.5: add 8 points.

- Do not open a second slot in a ticker already active unless score >= 85.

===============================================================
OUTPUT FORMAT
===============================================================

Return STRICT JSON (no prose, no markdown fence) with this exact shape:

{
  "decision": "TRADE" | "WATCH" | "SKIP",
  "score": 0-100,
  "entry_pattern": "two_alert_acceleration" | "halt_resume" | "news_catalyst" | "standard" | null,
  "entry":     <number>,
  "max_entry": <number>,
  "tp":        <number>,
  "stop":      <number>,
  "tp_pct":    <number>,
  "stop_pct":  <number>,
  "reason": "<one sentence referencing the specific context signals that drove this score>",
  "risk_flags": ["<flag>", ...]
}

ENTRY / MAX_ENTRY:
  - "entry"     = your IDEAL fill price (typically alert price × 1.03 for two-alert
                  acceleration, or current price for halt-resume / news catalyst).
  - "max_entry" = the WORST fill price you are willing to accept. The engine will
                  use this as the limit price during regular hours (09:30-16:00 ET)
                  so the order CANNOT fill above it.
                  - For two-alert acceleration: max_entry = entry × 1.02 (allow
                    2% slippage above your ideal entry — these run fast).
                  - For halt-resume: max_entry = entry × 1.03 (post-halt prints
                    can spike).
                  - For news catalyst: max_entry = entry × 1.04 (news is messy).
                  - For standard: max_entry = entry × 1.015 (tight — wait for
                    a clean fill, don't chase).
                  Outside regular hours the engine sends a market order instead
                  (T212 doesn't accept limits in pre/after-market) and max_entry
                  is informational only.

STOP LOSS (HARD CAP — 15%):
  The engine enforces a HARD floor: stop is never allowed to sit deeper than
  entry × 0.85 (i.e. max 15% loss). If you propose a wider stop it will be
  clamped silently. You can set it TIGHTER (e.g. 5-7% on fast_mover trades, or
  to alert1_price × 0.98 on two-alert acceleration setups) — that is preferred
  when structure supports it.

TAKE-PROFIT (tp_pct — YOU CHOOSE WITHIN BOUNDS):
  Set "tp_pct" to the **percent gain vs actual fill** you want for the resting
  take-profit / exit target. The execution layer clamps it to **[floor .. cap]**
  (defaults ~3% .. ~7.5% from server config). You **cannot** exceed the cap.
  - Quick headline / news-pop setups: often **3-4%** (easy, defined win).
  - Full runners / two-alert acceleration: use **near the cap** when justified.
  You should still fill "tp" as entry × (1 + tp_pct/100) for JSON consistency;
  the engine recomputes TP from the real fill using the resolved tp_pct.

Score >= 60 -> TRADE. 40-59 -> WATCH. Below 40 -> SKIP.
Score >= 80 -> HIGH CONVICTION.
If SKIP, entry/max_entry/tp/stop may be null.
"""

_SCORER_NEWS_SCANNER_SUPPLEMENT = """
===============================================================
PAYLOAD OVERRIDE — alert.type == "NEWS_SCANNER"
===============================================================
This alert came from the dedicated **#news-scanner** feed (headline-first, not the
main small-cap RV scanner). Strategy includes **scalping modest % moves on large-
and mega-cap names** after real catalysts (e.g. earnings, guidance) — **not**
only micro-cap momentum.

You MUST apply the following **instead of** the generic HARD DISCARD lines for
``float > 30M`` and ``market_cap > 200M`` on this request — those two caps are **void**
here. **Never** SKIP solely because market cap exceeds $200M or float exceeds 30M.

- **Never** emit risk flag ``market_cap_too_high`` or cite a "$200M hard discard"
  / "small-cap momentum only" narrative for this alert type.

Judge **headline materiality** (from ``alert.news_headline`` and ``news_class``) plus
``price_pack`` / tape: wide float and large mcap are **expected**, not disqualifying.
- Credible POSITIVE/SQUEEZE + orderly post-headline action → prefer **WATCH** (often
  46–58) on first evaluation; **TRADE** only with clear continuation, with **tp_pct**
  often **3–4%** (tight headline pop).
- Do **not** require two-alert KnownRunner acceleration or ``float < 10M`` unless this
  same symbol also appears as a regular scanner story in ``ticker_history``.
"""


async def score_alert(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the parsed JSON decision dict, or a SKIP fallback on failure."""

    def _run() -> dict[str, Any]:
        user_msg = "ALERT CONTEXT:\n" + json.dumps(payload, default=str)
        model = config.gemini_model_scorer()
        t_level = config.gemini_thinking_level()
        alert_obj = payload.get("alert") or {}
        system_instruction = _SCORER_SYSTEM
        if str(alert_obj.get("type") or "").upper() == "NEWS_SCANNER":
            system_instruction = _SCORER_SYSTEM + _SCORER_NEWS_SCANNER_SUPPLEMENT

        modes: list[bool] = []
        if config.gemini_scorer_try_thinking():
            modes.append(True)
        modes.append(False)

        last_preview = ""
        for enable_thinking in modes:
            try:
                txt = _gemini_single_response(
                    model=model,
                    system_instruction=system_instruction,
                    contents=user_msg,
                    max_output_tokens=8192,
                    response_json=True,
                    enable_thinking=enable_thinking,
                    thinking_level=t_level,
                    usage_call_kind="scorer",
                )
            except Exception as exc:
                _log.warning(
                    "scorer Gemini request failed thinking=%s: %s", enable_thinking, exc,
                )
                continue
            last_preview = txt or ""
            parsed = _parse_scorer_json(txt)
            if parsed is not None:
                return parsed
            _log.warning(
                "scorer JSON parse failed (thinking=%s) preview=%r",
                enable_thinking,
                (txt or "").strip()[:500],
            )

        _log.warning(
            "scorer gave no parseable JSON after %s attempt(s) tail=%r",
            len(modes),
            last_preview.strip()[-400:] if last_preview else "",
        )
        return {"decision": "SKIP", "score": 0, "reason": "scorer_json_parse_failed"}

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        _log.warning("score_alert failed: %s", exc)
        return {"decision": "SKIP", "score": 0, "reason": f"scorer_error:{exc}"[:200]}
