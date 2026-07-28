"""
Pluggable email sender for the daily pitch kit.

Set EMAIL_PROVIDER in .env to "resend" or "sendgrid". Both have free tiers:
  * Resend   -> https://resend.com   (simplest; free tier ~3,000 emails/mo)
  * SendGrid -> https://sendgrid.com (free up to 100 emails/day)

If no EMAIL_API_KEY is set, send_digest() becomes a no-op (and prints what it
would have sent), so the rest of the app still runs without email configured.

The email mirrors the on-site Daily Pitch Kit: the lead of the day (same backend
picker the site uses) with its thesis + talking points, a few more proactive targets,
and the top overnight headlines and filings.
"""
import html
import json
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

from . import config, database, spotlight, news

RESEND_URL = "https://api.resend.com/emails"
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

_BAND = [(75, "Severe"), (50, "High"), (25, "Elevated"), (0, "Moderate")]


def _esc(s):
    return html.escape(str(s or ""))


def _band(v):
    try:
        v = int(v or 0)
    except (ValueError, TypeError):
        v = 0
    for cut, name in _BAND:
        if v >= cut:
            return name
    return "Moderate"


def _eff_pitch(row):
    """Effective pitch for a row: AI-polished thesis/points if present, else templated."""
    try:
        p = json.loads(row.get("pitch") or "{}")
    except (ValueError, TypeError):
        p = {}
    ai = database.get_ai_pitch(row.get("cik")) or {}
    if ai.get("pitch"):
        try:
            a = json.loads(ai["pitch"])
        except (ValueError, TypeError):
            a = {}
        if a.get("thesis"):
            p = dict(p)
            p["thesis"] = a["thesis"]
            if a.get("points"):
                p["points"] = a["points"]
    return p


def build_digest_html(lead, targets, headlines, filings):
    site = config.SITE_URL.rstrip("/")

    # ---- Lead of the day -----------------------------------------------------
    if lead:
        lp = _eff_pitch(lead)
        thesis = _esc(lp.get("thesis") or lead.get("signals") or "")
        pts = "".join(
            f'<li style="margin:7px 0;line-height:1.45;">{_esc(p)}</li>'
            for p in (lp.get("points") or [])[:3]
        )
        pts_block = (f'<ol style="padding-left:20px;margin:12px 0 0;color:#1c1b18;font-size:14px;">{pts}</ol>'
                     if pts else "")
        lead_html = f"""
      <tr><td style="padding:0 0 6px;">
        <span style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#9a948a;">Lead of the day</span>
      </td></tr>
      <tr><td style="padding:0 0 4px;">
        <span style="font-family:Georgia,'Times New Roman',serif;font-size:23px;color:#1c1b18;">{_esc(lead.get('company'))}</span>
        <span style="color:#9a948a;font-size:15px;"> ({_esc(lead.get('ticker'))})</span>
      </td></tr>
      <tr><td style="padding:0 0 12px;">
        <span style="font-size:12px;font-weight:700;color:#15724e;">{_band(lead.get('vuln'))}</span>
        <span style="color:#9a948a;font-size:12px;"> &middot; matches the activist-target profile ({_esc(lead.get('vuln'))}/92)</span>
      </td></tr>
      <tr><td style="padding:0 0 6px;font-size:15px;line-height:1.5;color:#1c1b18;">{thesis}</td></tr>
      <tr><td>{pts_block}</td></tr>
      <tr><td style="padding:14px 0 0;">
        <a href="{site}/" style="background:#15724e;color:#fff;text-decoration:none;padding:9px 16px;border-radius:6px;font-size:13px;font-weight:600;">Open the pitch kit &rarr;</a>
      </td></tr>"""
    else:
        lead_html = '<tr><td style="color:#9a948a;">No lead of the day yet — check the dashboard.</td></tr>'

    # ---- More targets --------------------------------------------------------
    lead_cik = lead.get("cik") if lead else None
    more = [t for t in targets if t.get("cik") != lead_cik][:4]
    tg_rows = "".join(
        f'<tr>'
        f'<td style="padding:7px 10px;font-weight:600;color:#1c1b18;">{_esc(t.get("company"))} '
        f'<span style="color:#9a948a;font-weight:400;">({_esc(t.get("ticker"))})</span></td>'
        f'<td style="padding:7px 10px;text-align:center;color:#15724e;font-weight:700;white-space:nowrap;">{_band(t.get("vuln"))}</td>'
        f'<td style="padding:7px 10px;color:#5b554c;font-size:13px;">{_esc(t.get("signals"))}</td>'
        f'</tr>'
        for t in more
    ) or '<tr><td colspan="3" style="padding:7px 10px;color:#9a948a;">No additional targets today.</td></tr>'

    # ---- Headlines + filings -------------------------------------------------
    news_rows = "".join(
        f'<li style="margin:6px 0;line-height:1.4;"><a href="{_esc(h["url"])}" style="color:#2f5fa6;text-decoration:none;">{_esc(h["headline"])}</a>'
        f' <span style="color:#9a948a;">— {_esc(h.get("source"))}</span></li>'
        for h in (headlines or [])[:6]
    ) or '<li style="color:#9a948a;">No notable headlines.</li>'

    filing_rows = "".join(
        f'<li style="margin:6px 0;line-height:1.4;"><a href="{_esc(f["url"])}" style="color:#2f5fa6;text-decoration:none;">{_esc(f.get("company"))} — {_esc(f.get("title"))}</a>'
        f' <span style="color:#9a948a;">({_esc(f.get("form"))})</span></li>'
        for f in (filings or [])[:5]
    ) or '<li style="color:#9a948a;">No notable filings.</li>'

    return f"""\
<div style="font-family:Helvetica,Arial,sans-serif;max-width:680px;margin:auto;background:#f6f4ee;padding:26px;color:#1c1b18;">
  <div style="font-family:Georgia,serif;font-size:13px;letter-spacing:.04em;color:#15724e;">FGS GLOBAL &middot; DAILY PITCH KIT</div>
  <div style="color:#9a948a;font-size:12px;margin:2px 0 18px;">Proactive activist-vulnerability leads &middot; generated automatically each morning</div>

  <table role="presentation" width="100%" style="background:#fffdf8;border:1px solid #e7e2d8;border-radius:10px;padding:20px;border-collapse:separate;">
    {lead_html}
  </table>

  <h3 style="font-family:Georgia,serif;font-size:15px;margin:24px 0 6px;color:#1c1b18;">More proactive targets</h3>
  <table role="presentation" width="100%" style="border-collapse:collapse;font-size:14px;background:#fffdf8;border:1px solid #e7e2d8;border-radius:10px;overflow:hidden;">
    <tbody>{tg_rows}</tbody>
  </table>

  <h3 style="font-family:Georgia,serif;font-size:15px;margin:24px 0 6px;color:#1c1b18;">Top headlines</h3>
  <ul style="padding-left:18px;margin:0;font-size:14px;">{news_rows}</ul>

  <h3 style="font-family:Georgia,serif;font-size:15px;margin:24px 0 6px;color:#1c1b18;">Recent filings</h3>
  <ul style="padding-left:18px;margin:0;font-size:14px;">{filing_rows}</ul>

  <p style="color:#9a948a;font-size:11px;margin-top:26px;line-height:1.5;border-top:1px solid #e7e2d8;padding-top:14px;">
    Internal new-business tool for FGS Global. Predictive screen built on public data — it can
    misflag, and is not a substitute for FactSet/Diligent or legal/financial advice. Not for client distribution.
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


def _send_gmail(to_email, subject, html_body):
    """Send via Gmail SMTP using an App Password. No domain/DNS setup required."""
    user = config.GMAIL_USER
    pw = config.GMAIL_APP_PASSWORD
    if not user or not pw:
        return False, "GMAIL_USER / GMAIL_APP_PASSWORD not set"
    # From address: prefer an explicit EMAIL_FROM, but Gmail will rewrite the
    # envelope sender to the authenticated account anyway, so fall back to it.
    from_addr = config.EMAIL_FROM
    if not from_addr or from_addr == "digest@example.com":
        from_addr = user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.EMAIL_FROM_NAME, from_addr))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.sendmail(user, [to_email], msg.as_string())
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - surface any SMTP/auth error in the log
        return False, str(e)


def send_one(to_email, subject, html_body):
    provider = config.EMAIL_PROVIDER
    # Gmail uses an App Password (GMAIL_APP_PASSWORD), not EMAIL_API_KEY.
    if provider == "gmail":
        if not config.GMAIL_APP_PASSWORD:
            print(f"[email] (disabled) would send '{subject}' to {to_email}")
            return False
        ok, msg = _send_gmail(to_email, subject, html_body)
    elif not config.EMAIL_API_KEY:
        print(f"[email] (disabled) would send '{subject}' to {to_email}")
        return False
    elif provider == "sendgrid":
        ok, msg = _send_sendgrid(to_email, subject, html_body)
    else:
        ok, msg = _send_resend(to_email, subject, html_body)
    if not ok:
        print(f"[email] FAILED to {to_email}: {msg[:200]}")
    return ok


def _build_digest():
    """Assemble the daily pitch-kit body + subject once, so send_digest() (all subscribers) and
    send_test_digest() (one address) render byte-identical email and can never drift. Returns
    (subject, html_body)."""
    rows = database.get_scores(limit=80)
    lead = spotlight.todays_lead(rows, database)
    # Curated + ranked by activist-relevance (ownership/activism first), not just most-recent, so
    # the email leads with smart-money / campaign items rather than generic market headlines.
    headlines = news.rank_relevant(database.recent_news(limit=30, relevant_only=True), 6)
    filings = database.recent_filings(limit=5)
    body = build_digest_html(lead, rows, headlines, filings)
    subject = "FGS — Daily Pitch Kit"
    if lead:
        subject += f": {lead.get('company')}"
    return subject, body


def send_digest():
    """Build today's emailed pitch kit and send to every subscriber. Returns count sent."""
    subs = database.get_subscribers()
    if not subs:
        print("[email] no subscribers; nothing to send")
        return 0
    subject, body = _build_digest()
    sent = 0
    for email in subs:
        if send_one(email, subject, body):
            sent += 1
    print(f"[email] pitch kit sent to {sent}/{len(subs)} subscribers")
    return sent


def send_test_digest(to_email):
    """Send the daily pitch-kit digest to exactly one address. Mirrors send_test_report's guard:
    a missing/blank/list `to_email` is a hard no-op rather than a silent fallback to the whole
    subscriber list, and the subject is tagged [TEST]. Same real digest body as send_digest();
    only the audience differs. Returns True/False."""
    if not to_email or not isinstance(to_email, str) or "@" not in to_email or "," in to_email:
        print(f"[email] send_test_digest needs one explicit recipient; got: {to_email!r}")
        return False
    subject, body = _build_digest()
    ok = send_one(to_email, f"[TEST] {subject}", body)
    print(f"[email] test digest to {to_email}: {'ok' if ok else 'FAILED'}")
    return ok


def build_report_html():
    """Assemble + render the biweekly report HTML for the /report web page + browser preview.
    Uses report.render_html() -- the CSS-grid version -- since a browser tab has no need for the
    table-layout/inline-style contortions email clients require. NOT what gets emailed; see
    build_report_email_html() / send_report() for that."""
    from . import report, catalyst, aithesis
    model = report.assemble(database, catalyst, news,
                            limit=config.REPORT_BOARD_SIZE,
                            summarize=aithesis.summarize_line)
    return report.render_html(model)


def _assemble_report_model():
    from . import report, catalyst, aithesis
    return report.assemble(database, catalyst, news,
                           limit=config.REPORT_BOARD_SIZE,
                           summarize=aithesis.summarize_line)


def build_report_email_html(model=None):
    """Render the biweekly report the way it actually goes out: inline styles + <table> layout,
    with internal links resolved to absolute via SITE_URL. Same renderer and same model as the
    web page -- only the `email=True` flag differs -- because <style> blocks and CSS grid get
    stripped or mangled by Gmail/Outlook/Apple Mail, so the inbox needs its own markup even
    though the content is identical."""
    from . import report
    model = model or _assemble_report_model()
    return report.render_html(model, email=True, site_url=config.SITE_URL)


def send_report(recipients=None):
    """Build the biweekly report and email it. Recipients default to the configured report list,
    else the subscriber list (the same small distribution as the paused daily digest). Returns
    the count sent. Safe: no-op with a printed notice when there are no recipients."""
    to = recipients or config.REPORT_RECIPIENTS or database.get_subscribers()
    if not to:
        print("[report] no recipients; nothing to send")
        return 0
    model = _assemble_report_model()
    body = build_report_email_html(model)
    subject = f"FGS — Activist Vulnerability (biweekly) · {model.get('issue_date')}"
    sent = 0
    for email in to:
        if send_one(email, subject, body):
            sent += 1
    print(f"[report] biweekly report sent to {sent}/{len(to)}")
    return sent


def send_test_report(to_email):
    """Send the biweekly report to exactly one address, for previewing the real email render
    before it goes to the distribution list. Guard: this never falls back to REPORT_RECIPIENTS
    or the subscriber list the way send_report() does -- a missing/blank `to_email` is a hard
    no-op rather than silently reusing the production list, and the subject is tagged [TEST] so
    a test send can never be mistaken for the real issue in someone's inbox. Returns True/False."""
    if not to_email or not isinstance(to_email, str) or "@" not in to_email:
        print("[report] send_test_report requires a single explicit recipient email; got: "
              f"{to_email!r}")
        return False
    if "," in to_email:
        print("[report] send_test_report takes exactly one address, not a list; got: "
              f"{to_email!r}")
        return False
    model = _assemble_report_model()
    body = build_report_email_html(model)
    subject = f"[TEST] FGS — Activist Vulnerability (biweekly) · {model.get('issue_date')}"
    ok = send_one(to_email, subject, body)
    print(f"[report] test send to {to_email}: {'ok' if ok else 'FAILED'}")
    return ok


def generate_issue(today=None):
    """GENERATE + AUDIT (Tuesday night). Assemble the rotated, no-repeat issue, render the exact
    email body, run the auto-audit, and FREEZE it in report_history so Wednesday sends precisely
    what was audited. Returns the audit result dict. Never sends here."""
    from . import report, catalyst, aithesis, report_audit, credibility
    from datetime import datetime
    today = today or datetime.utcnow()
    issue_id = today.date().isoformat()
    model = report.assemble(database, catalyst, news, limit=config.REPORT_BOARD_SIZE,
                            today=today, summarize=aithesis.summarize_line, rotate=True)
    body = build_report_email_html(model)
    result = report_audit.audit(model, credibility=credibility)
    tickers = [c.get("ticker") for c in (model.get("board") or []) if c.get("ticker")]
    subject = f"FGS — Activist Vulnerability (biweekly) · {model.get('issue_date')}"
    database.insert_report_issue(issue_id, tickers, result["status"], result["flags"], subject, body)
    print(f"[report] generated issue {issue_id}: {tickers} · audit {result['summary']}", flush=True)
    return result


def send_pending_issue(max_age_hours=40):
    """SEND (Wednesday morning). Ship the most recent generated-but-unsent issue to the distribution
    list ONLY when the owner has APPROVED it (status 'approved', set via /api/report/approve after
    the Tuesday-night review). If the audit HELD it, alert the admin instead; if it's clean but not
    yet approved (or the owner declined), hold and mail no one. Only acts on a FRESH issue (generated
    within ~40h) so a stale pending never re-fires. Returns the number of subscribers sent to."""
    from datetime import datetime, timedelta
    issue = database.get_pending_issue()
    if not issue:
        print("[report] no pending issue to send")
        return 0
    try:
        gen_dt = datetime.fromisoformat((issue.get("generated_at") or "").replace("Z", ""))
        if datetime.utcnow() - gen_dt > timedelta(hours=max_age_hours):
            print(f"[report] pending issue {issue.get('issue_id')} is stale; skipping", flush=True)
            return 0
    except Exception:
        pass

    issue_id = issue.get("issue_id")
    status = (issue.get("audit_status") or "").lower()

    if status == "held":
        # audit HELD — never mail the list. Alert ONLY the configured admin (John); NEVER fall back
        # to a subscriber, so a hold notice can never reach the FGS partners. If admin is blank, skip
        # the alert entirely (log only) rather than risk it.
        admin = (config.REPORT_ADMIN_EMAIL or "").strip()
        try:
            flags = json.loads(issue.get("audit_flags") or "[]")
        except Exception:
            flags = []
        if admin:
            items = "".join(
                f"<li>[{f.get('severity')}] {f.get('check')} — {f.get('subject')}: "
                f"{html.escape(str(f.get('detail') or ''))}</li>" for f in flags)
            alert = (f"<p>The biweekly issue <b>{issue_id}</b> ({html.escape(issue.get('tickers') or '')}) "
                     f"was <b>HELD</b> by the auto-audit and was <b>not</b> sent to the distribution "
                     f"list. Review the flags below; once resolved it can be re-generated / released "
                     f"manually.</p><ul>{items or '<li>(no detail)</li>'}</ul>")
            send_one(admin, f"[HELD] FGS biweekly {issue_id} — audit flagged, not sent", alert)
        database.mark_issue_sent(issue_id)          # consumed for this cycle (alert delivered)
        who = f"alerted {admin}" if admin else "NO admin configured — alert skipped"
        print(f"[report] issue {issue_id} HELD by audit; {who}; NOT sent to list", flush=True)
        return 0

    if status != "approved":
        # 'clean' = passed the audit but AWAITING the owner's approval; 'held_user' = owner declined.
        # Do NOT send and do NOT consume it — the owner approves via /api/report/approve, and the
        # next 7 AM send window then ships it. A stale unapproved issue is skipped by the freshness
        # guard above, so it never lingers or goes out without a human OK.
        print(f"[report] issue {issue_id} not approved (status={status or 'clean'}); "
              f"awaiting owner approval — NOT sent", flush=True)
        return 0

    # APPROVED by the owner — send the frozen, audited body to the distribution list (subscribers).
    to = config.REPORT_RECIPIENTS or database.get_subscribers()
    if not to:
        print("[report] clean issue but no recipients; nothing to send")
        return 0
    subject, body = issue.get("subject"), issue.get("html")
    sent = sum(1 for e in to if send_one(e, subject, body))
    database.mark_issue_sent(issue_id)
    print(f"[report] issue {issue_id} sent to {sent}/{len(to)} subscribers", flush=True)
    return sent
