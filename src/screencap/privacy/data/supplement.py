"""Curated domain supplement for privacy classification.

Covers domains not in the UT1 blocklist: password manager web vaults,
admin consoles, cloud storage, video call, calendar, code platforms,
and gap-fills for email/chat providers missing from UT1.

Supplement entries override UT1 on conflicts (by design — curated
classifications are more accurate than automated blocklist categories).
"""

from __future__ import annotations

# Lazy import — ContextClass is resolved at load time by domain_loader.
# Using strings here avoids a circular import; domain_loader maps them.
# Each value is a ContextClass enum member name (uppercase).

SUPPLEMENT: dict[str, str] = {
    # ── Password managers (browser web vaults) ──────────────────────
    "vault.bitwarden.com": "PASSWORD_MANAGER",
    "my.1password.com": "PASSWORD_MANAGER",
    "app.dashlane.com": "PASSWORD_MANAGER",
    "lastpass.com": "PASSWORD_MANAGER",
    "app.nordpass.com": "PASSWORD_MANAGER",
    "app.keepersecurity.com": "PASSWORD_MANAGER",
    "app.proton.me/pass": "PASSWORD_MANAGER",  # Proton Pass web
    # ── Admin consoles ──────────────────────────────────────────────
    "console.aws.amazon.com": "ADMIN_CONSOLE",
    "console.cloud.google.com": "ADMIN_CONSOLE",
    "portal.azure.com": "ADMIN_CONSOLE",
    "vercel.com": "ADMIN_CONSOLE",
    "dashboard.heroku.com": "ADMIN_CONSOLE",
    "app.netlify.com": "ADMIN_CONSOLE",
    "dash.cloudflare.com": "ADMIN_CONSOLE",
    "cloud.digitalocean.com": "ADMIN_CONSOLE",
    "app.datadoghq.com": "ADMIN_CONSOLE",
    "app.newrelic.com": "ADMIN_CONSOLE",
    "app.pagerduty.com": "ADMIN_CONSOLE",
    "app.sentry.io": "ADMIN_CONSOLE",
    "grafana.com": "ADMIN_CONSOLE",
    # ── Cloud storage ───────────────────────────────────────────────
    "drive.google.com": "CLOUD_STORAGE",
    "dropbox.com": "CLOUD_STORAGE",
    "onedrive.live.com": "CLOUD_STORAGE",
    "box.com": "CLOUD_STORAGE",
    "icloud.com": "CLOUD_STORAGE",
    "mega.nz": "CLOUD_STORAGE",
    "sync.com": "CLOUD_STORAGE",
    "pcloud.com": "CLOUD_STORAGE",
    # ── Video call ──────────────────────────────────────────────────
    "meet.google.com": "VIDEO_CALL",
    "zoom.us": "VIDEO_CALL",
    "app.zoom.us": "VIDEO_CALL",
    "app.webex.com": "VIDEO_CALL",
    "whereby.com": "VIDEO_CALL",
    "around.co": "VIDEO_CALL",
    # ── Calendar ────────────────────────────────────────────────────
    "calendar.google.com": "CALENDAR",
    "outlook.office.com": "CALENDAR",  # calendar view shares this domain
    # ── Code platforms ──────────────────────────────────────────────
    "github.com": "CODE_EDITOR_TERMINAL",
    "gitlab.com": "CODE_EDITOR_TERMINAL",
    "bitbucket.org": "CODE_EDITOR_TERMINAL",
    "codepen.io": "CODE_EDITOR_TERMINAL",
    "replit.com": "CODE_EDITOR_TERMINAL",
    "codesandbox.io": "CODE_EDITOR_TERMINAL",
    "stackblitz.com": "CODE_EDITOR_TERMINAL",
    # ── Email (UT1 gaps) ───────────────────────────────────────────
    "outlook.live.com": "EMAIL",
    "outlook.office365.com": "EMAIL",
    "mail.proton.me": "EMAIL",
    "app.fastmail.com": "EMAIL",
    "tutanota.com": "EMAIL",
    "mail.zoho.com": "EMAIL",
    "app.hey.com": "EMAIL",
    "mail.aol.com": "EMAIL",
    "mail.icloud.com": "EMAIL",
    # ── Chat (UT1 gaps) ────────────────────────────────────────────
    "app.slack.com": "CHAT",
    "web.whatsapp.com": "CHAT",
    "web.telegram.org": "CHAT",
    "app.element.io": "CHAT",
    "app.zulip.com": "CHAT",
    "mattermost.com": "CHAT",
    # ── Banking (UT1 gaps — subdomains UT1 misses) ─────────────────
    "secure.bankofamerica.com": "BANKING",
    "online.citi.com": "BANKING",
}
