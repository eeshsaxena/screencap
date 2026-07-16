"""Branded HTML for the loopback OAuth callback page (Fieldnotes theme).

Rendered on the user's OWN machine at ``http://127.0.0.1:<port>`` the instant
Google redirects back from sign-in. Two rules shape it:

* **Fully self-contained** — no external fonts, scripts, images, or stylesheets.
  A privacy-focused tool must not fire a silent third-party request from its
  sign-in page, and loopback has no guaranteed network anyway. Brand webfonts
  are named first in each stack so they're used *if already installed*, with
  graceful system fallbacks otherwise.
* **Mirrors screencap.sh** — colors, typography, logo mark, and the centred-card
  layout track the website's "Fieldnotes" system (the checkout return page in
  ``screencap-website`` is the visual reference). Success and failure get
  distinct, honest states.
"""

from __future__ import annotations

import html

# Brand mark: two rounded crop brackets holding a record dot (viewBox 0 0 48 48,
# geometry fixed per the brand board). Stroke follows --ink, dot follows --teal,
# so both flip in dark mode.
_LOGO = (
    '<svg width="22" height="22" viewBox="0 0 48 48" role="img" aria-label="ScreenCap">'
    '<path d="M 20 6 L 12 6 Q 6 6 6 12 L 6 20" fill="none" stroke="var(--ink)"'
    ' stroke-width="4.5" stroke-linecap="round"/>'
    '<path d="M 28 42 L 36 42 Q 42 42 42 36 L 42 28" fill="none" stroke="var(--ink)"'
    ' stroke-width="4.5" stroke-linecap="round"/>'
    '<circle cx="24" cy="24" r="8.5" fill="var(--teal)"/></svg>'
)

_CHECK_ICON = (
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" stroke-width="2.2"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_CROSS_ICON = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<path d="M7 7l10 10M17 7L7 17" stroke="currentColor" stroke-width="2.2"'
    ' stroke-linecap="round"/></svg>'
)

# Placeholder tokens (not str.format) so the CSS ``{ }`` braces pass through raw.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE_TAB__</title>
<style>
:root {
  --ground:#f6f2e9; --surface:#fffdf7; --ink:#1c2420; --ink-muted:#52584f;
  --meta:#86795f; --teal:#0e7c6b; --border:#e0d8c6;
  --serif:"Newsreader",ui-serif,Georgia,"Times New Roman",serif;
  --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace;
  --sans:"Public Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ground:#10161a; --surface:#1c2420; --ink:#ede6d6; --ink-muted:#9ba69e;
    --meta:#5c6b68; --teal:#2fa08d; --border:#2a322e;
  }
}
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body {
  display:flex; align-items:center; justify-content:center;
  background:var(--ground); color:var(--ink); font-family:var(--sans);
  padding:2.5rem 1.5rem; -webkit-font-smoothing:antialiased;
}
.card {
  width:100%; max-width:440px; background:var(--surface);
  border:1px solid var(--border); border-radius:16px; padding:2.25rem; text-align:center;
}
.logo { margin-bottom:1.75rem; display:flex; justify-content:center; }
.badge {
  width:56px; height:56px; margin:0 auto 1.5rem; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
}
.tone-success .badge { background:rgba(14,124,107,.10); color:var(--teal); }
.tone-error .badge { background:rgba(28,36,32,.05); color:var(--meta); }
@media (prefers-color-scheme: dark) {
  .tone-success .badge { background:rgba(47,160,141,.14); }
  .tone-error .badge { background:rgba(237,230,214,.06); }
}
.eyebrow { margin:0; font-family:var(--mono); font-size:11.5px; letter-spacing:.08em; color:var(--meta); }
h1 { margin:.75rem 0 0; font-family:var(--serif); font-size:30px; font-weight:500; line-height:1.15; color:var(--ink); }
.lines { margin-top:1rem; display:flex; flex-direction:column; gap:.5rem; }
.lines p { margin:0; font-size:14.5px; line-height:1.65; color:var(--ink-muted); }
</style>
</head>
<body class="tone-__TONE__">
<main class="card">
  <div class="logo">__LOGO__</div>
  <div class="badge">__ICON__</div>
  <p class="eyebrow">__EYEBROW__</p>
  <h1>__TITLE__</h1>
  <div class="lines">__LINES__</div>
</main>
</body>
</html>"""


def render_callback_page(*, success: bool) -> bytes:
    """Render the loopback sign-in page as UTF-8 bytes.

    ``success`` picks the honest state: the teal check + "you're set" copy, or the
    muted cross + "try again" copy for a declined/failed callback.
    """
    if success:
        tone, title_tab = "success", "Signed in — ScreenCap"
        icon, eyebrow, title = _CHECK_ICON, "SIGNED IN", "You're all set."
        lines = [
            "ScreenCap now has your account.",
            "You can close this tab and return to the app.",
        ]
    else:
        tone, title_tab = "error", "Sign-in failed — ScreenCap"
        icon, eyebrow, title = _CROSS_ICON, "SIGN-IN FAILED", "Sign-in didn't finish."
        lines = [
            "No account was connected.",
            "You can close this tab and try again from ScreenCap.",
        ]

    lines_html = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    page = (
        _TEMPLATE.replace("__TITLE_TAB__", html.escape(title_tab))
        .replace("__TONE__", tone)
        .replace("__LOGO__", _LOGO)
        .replace("__ICON__", icon)
        .replace("__EYEBROW__", html.escape(eyebrow))
        .replace("__TITLE__", html.escape(title))
        .replace("__LINES__", lines_html)
    )
    return page.encode("utf-8")
