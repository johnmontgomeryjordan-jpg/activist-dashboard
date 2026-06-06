"""
Pluggable email sender for the daily digest.

Set EMAIL_PROVIDER in .env to "resend" or "sendgrid". Both have free tiers:
  * Resend   -> https://resend.com   (simplest; free tier ~3,000 emails/mo)
  * SendGrid -> https://sendgrid.com (free up to 100 emails/day)

If no EMAIL_API_KEY is set, send_digest() becomes a no-op (and prints what it
would have sent), so the rest of the app still runs without email configured.
"""
import html

import requests

from . import config, database

RESEND_URL = "https://api.resend.com/emails"
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def build_digest_html(headlines, companies):
    """Render the digest body: top 5 headlines + top 5 companies."""
    def esc(s):
        return html.escape(str(s or ""))

    rows_news = "".join(
        f'<li style="margin:6px 0;"><a href="{esc(h["url"])}">{esc(h["headline"])}</a>'
        f' <span style="color:#888;">— {esc(h["source"])}</span></li>'
        for h in headlines[:5]
    ) or "<li>No relevant headlines today.</li>"

    rows_co = ""
    for c in companies[:5]:
        link = (f' &middot; <a href="{esc(c["top_item_url"])}">latest</a>'
                if c.get("top_item_url") else "")
        rows_co += (
            f'<tr>'
            f'<td style="padding:6px 10px;font-weight:600;">{esc(c["company"])} '
            f'({esc(c["ticker"])})</td>'
            f'<td style="padding:6px 10px;text-align:center;">{esc(c["score"])}</td>'
            f'<td style="padding:6px 10px;color:#444;">{esc(c["signals"])}{link}</td>'
            f'</tr>'
        )
    rows_co = rows_co or '<tr><td colspan="3" style="padding:6px 10px;">No flagged companies today.</td></tr>'

    return f"""\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:auto;color:#1a1a1a;">
  <h2 style="margin-bottom:2px;">Activist Vulnerability — Daily Digest</h2>
  <p style="color:#888;margin-top:0;">Generated automatically. Links open the original sources.</p>

  <h3 style="border-bottom:2px solid #eee;padding-bottom:4px;">Top headlines</h3>
  <ul style="padding-left:18px;">{rows_news}</ul>

  <h3 style="border-bottom:2px solid #eee;padding-bottom:4px;">Companies to pitch</h3>
  <table style="border-collapse:collapse;width:100%;font-size:14px;">
    <thead>
      <tr style="background:#f5f5f5;text-align:left;">
        <th style="padding:6px 10px;">Company</th>
        <th style="padding:6px 10px;">Score</th>
        <th style="padding:6px 10px;">Signals</th>
      </tr>
    </thead>
    <tbody>{rows_co}</tbody>
  </table>

  <p style="color:#aaa;font-size:12px;margin-top:24px;">
    You are receiving this because you subscribed on the dashboard. This is a
    proof-of-concept demo for internal use.
  </p>
</div>"""


def _send_resend(to_email, subject, html_body):
    headers = {"Authorization": f"Bearer {config.EMAIL_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "from": f"{config.EMAIL_FROM_NAME} <{config.EMAIL_FROM}>",
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    r = requests.post(RESEND_URL, json=payload, headers=headers, timeout=20)
    return r.status_code in (200, 201), r.text


def _send_sendgrid(to_email, subject, html_body):
    headers = {"Authorization": f"Bearer {config.EMAIL_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": config.EMAIL_FROM, "name": config.EMAIL_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    r = requests.post(SENDGRID_URL, json=payload, headers=headers, timeout=20)
    return r.status_code in (200, 202), r.text


def send_one(to_email, subject, html_body):
    if not config.EMAIL_API_KEY:
        print(f"[email] (disabled) would send '{subject}' to {to_email}")
        return False
    if config.EMAIL_PROVIDER == "sendgrid":
        ok, msg = _send_sendgrid(to_email, subject, html_body)
    else:
        ok, msg = _send_resend(to_email, subject, html_body)
    if not ok:
        print(f"[email] FAILED to {to_email}: {msg[:200]}")
    return ok


def send_digest():
    """Build today's digest and send to every subscriber. Returns count sent."""
    subs = database.get_subscribers()
    if not subs:
        print("[email] no subscribers; nothing to send")
        return 0
    headlines = database.recent_news(limit=5)
    companies = database.get_scores(limit=5)
    body = build_digest_html(headlines, companies)
    subject = "Activist Vulnerability — Daily Digest"
    sent = 0
    for email in subs:
        if send_one(email, subject, body):
            sent += 1
    print(f"[email] digest sent to {sent}/{len(subs)} subscribers")
    return sent
