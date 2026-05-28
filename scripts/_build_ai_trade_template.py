#!/usr/bin/env python3
"""Build AI_Trade_Program/templates/ai_trade.html from git HEAD tradingserver.html."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AITP_ROOT = Path(__file__).resolve().parents[1]
TRADING_REPO = AITP_ROOT.parent / "Trading Platform"
OUT = AITP_ROOT / "templates" / "ai_trade.html"


def main() -> None:
    raw = subprocess.check_output(
        ["git", "show", "HEAD:Main_Website/tradingserver.html"],
        cwd=TRADING_REPO,
        text=True,
    )

    body_start = raw.index("<body>")
    header_start = raw.index("<!-- HEADER -->")
    pre_header = raw[body_start:header_start]

    nav = (
        '  <div class="header-center" id="navTabs">\n'
        '    <button type="button" class="nav-tab active" data-nav="ai-trade" '
        'aria-label="AI Trade" aria-current="page">'
        '<span class="nav-tab-full">AI Trade</span>'
        '<span class="nav-tab-mini" aria-hidden="true">AI</span></button>\n'
        '    <button type="button" class="nav-tab" data-nav="reasoning-test" '
        'aria-label="Reasoning Test">'
        '<span class="nav-tab-full">Reasoning Test</span>'
        '<span class="nav-tab-mini" aria-hidden="true">Lab</span></button>\n'
        '  </div>\n'
    )

    header_end = raw.index("</header>", header_start) + len("</header>")
    header_html = raw[header_start:header_end]
    header_html = re.sub(
        r'<div class="header-center" id="navTabs">.*?</div>\s*\n',
        nav,
        header_html,
        count=1,
        flags=re.DOTALL,
    )
    header_html = header_html.replace(
        '<a class="icon-link" href="/logout" title="Sign out"><i class="ti ti-logout"></i></a>\n',
        "",
    )

    ms = "<!-- ═══════════════════════════════════════════════════════════\n     AI TRADE PAGE"
    me = "</div><!-- /apex-wrap -->"
    i0 = raw.index(ms)
    i1 = raw.index(me, i0)
    ai_block = raw[i0:i1]

    script_full = raw.split("<script>", 1)[1].split("</script>", 1)[0]

    def cut(s: str, a: str, b: str) -> str:
        i, j = s.find(a), s.find(b, s.find(a))
        if i == -1 or j == -1:
            raise SystemExit(f"cut failed {a!r} {b!r}")
        return s[i:j]

    reason_ai_js = cut(
        script_full,
        "function rtBuildPathWaypointGrid()",
        "/* ─── HELPERS shared across pages ──────────────────────────── */",
    )

    def fn_slice(s: str, name: str) -> str:
        i = s.find(name)
        if i == -1:
            raise SystemExit(f"missing {name}")
        j = s.find("\n}", i)
        if j == -1:
            raise SystemExit("no closing brace")
        return s[i : j + 2]

    dollar_fn = fn_slice(script_full, "function $(id)")
    escape_fn = fn_slice(script_full, "function escapeHtml(")

    nav_js = r"""
let _currentPage = 'ai-trade';
let _reasoningAbort = null;
const NAV_TAB_STORAGE_KEY = 'pulse_ai_nav_tab';
const NAV_TAB_IDS = new Set(['ai-trade', 'reasoning-test']);

function restoreNavTabIfStored() {
  let name = null;
  try { name = localStorage.getItem(NAV_TAB_STORAGE_KEY); } catch (_) { return; }
  if (!name || !NAV_TAB_IDS.has(name)) return;
  const tab = document.querySelector('#navTabs .nav-tab[data-nav="' + name + '"]');
  if (!tab) return;
  document.querySelectorAll('#navTabs .nav-tab').forEach((t) => {
    t.classList.remove('active');
    t.removeAttribute('aria-current');
  });
  tab.classList.add('active');
  tab.setAttribute('aria-current', 'page');
  showPage(name);
}

function showPage(name) {
  _currentPage = name;
  document.querySelectorAll('.page-view').forEach((el) => el.classList.remove('page-active'));
  const pg = document.getElementById('page-' + name);
  if (pg) {
    pg.classList.add('page-active');
    if (NAV_TAB_IDS.has(name)) {
      try { localStorage.setItem(NAV_TAB_STORAGE_KEY, name); } catch (_) {}
    }
  }
  if (name === 'ai-trade') renderAITradePage();
  if (_reasoningAbort && name !== 'reasoning-test') {
    try { _reasoningAbort.abort(); } catch (_) {}
    _reasoningAbort = null;
  }
}
"""

    boot = f"""
document.addEventListener('DOMContentLoaded', () => {{
{dollar_fn}
{escape_fn}
{nav_js}
  document.querySelectorAll('#navTabs .nav-tab').forEach((tab) => {{
    tab.addEventListener('click', () => {{
      const nav = tab.getAttribute('data-nav');
      document.querySelectorAll('#navTabs .nav-tab').forEach((t) => {{
        t.classList.remove('active');
        t.removeAttribute('aria-current');
      }});
      tab.classList.add('active');
      tab.setAttribute('aria-current', 'page');
      showPage(nav);
    }});
  }});
  bindReasoningTestPage();
  restoreNavTabIfStored();
}});
"""

    head = raw[:body_start]
    head = head.replace(
        "<title>APEX - Intelligence</title>",
        "<title>AI Trade · Sandbox</title>",
    )

    html = (
        head
        + pre_header
        + header_html
        + "\n"
        + ai_block
        + "\n</div><!-- /apex-wrap -->\n<script>\n"
        + reason_ai_js
        + boot
        + "\n</script>\n</body>\n</html>\n"
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("Wrote", OUT, len(html.encode("utf-8")), "bytes")


if __name__ == "__main__":
    main()
