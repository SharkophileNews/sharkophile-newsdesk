"""Editor alerts: Slack incoming webhook, email (SMTP) and a generic JSON webhook."""

from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _status_line(d: dict) -> str:
    fc = d.get("fact_check", {}).get("verdict", "n/a")
    icon = {"pass": "✅", "minor_issues": "🟡", "major_issues": "🔴"}.get(fc, "⚪")
    return f"{icon} Fact-check: {fc.replace('_', ' ')} · SEO {d.get('seo_score', '–')}/100 · {d.get('word_count', 0)} words"


def _headline(run: dict) -> str:
    if run.get("fatal"):
        return "⚠️ Sharkophile newsdesk run failed"
    n = len(run["drafts"])
    return f"🦈 {n} Sharkophile draft{'s' if n != 1 else ''} ready for review" if n else \
        "🦈 Sharkophile newsdesk: no new drafts this run"


def summary_text(run: dict) -> str:
    lines = [_headline(run), ""]
    if run.get("fatal"):
        lines += [f"Error: {run['fatal']}", "Nothing was drafted. Check the run log in GitHub Actions.", ""]
    for d in run["drafts"]:
        lines += [
            f"■ {d['headline']}",
            f"  {_status_line(d)}",
            f"  Categories: {', '.join(d['categories'])} | Focus: {d.get('focus_keyword','')}",
            f"  Edit: {d.get('edit_link','(dry run)')}",
            f"  Preview: {d.get('preview_link','')}",
        ]
        for w in d.get("warnings", []):
            lines.append(f"  ⚠ {w}")
        if d.get("editor_notes"):
            lines.append(f"  Notes: {d['editor_notes']}")
        lines.append("  Sources: " + ", ".join(s.get("url", "") for s in d.get("sources", [])[:4]))
        lines.append("")
    for e in run.get("errors", []):
        lines.append(f"✖ Skipped: {e}")
    return "\n".join(lines)


def summary_html(run: dict) -> str:
    parts = [f"<h2 style='font-family:sans-serif'>{html.escape(_headline(run))}</h2>"]
    if run.get("fatal"):
        parts.append(f"<p style='font-family:sans-serif;color:#b91c1c'><b>Error:</b> {html.escape(run['fatal'])}"
                     "<br>Nothing was drafted. Check the run log in GitHub Actions.</p>")
    for d in run["drafts"]:
        warn = "".join(f"<li>⚠ {html.escape(w)}</li>" for w in d.get("warnings", []))
        srcs = "".join(f"<li><a href='{html.escape(s.get('url',''))}'>{html.escape(s.get('publisher') or s.get('url',''))}</a></li>"
                       for s in d.get("sources", [])[:5])
        img = (f"<img src='{html.escape(d['image_url'])}' alt='' width='480' style='max-width:100%;border-radius:6px'><br>"
               if d.get("image_url") else "")
        parts.append(
            "<div style='font-family:sans-serif;border:1px solid #d0d7de;border-radius:8px;padding:16px;margin:16px 0'>"
            f"{img}<h3 style='margin:8px 0'>{html.escape(d['headline'])}</h3>"
            f"<p style='color:#555;margin:4px 0'>{html.escape(d.get('meta_description',''))}</p>"
            f"<p>{html.escape(_status_line(d))}<br>Categories: {html.escape(', '.join(d['categories']))}"
            f" · Tags: {html.escape(', '.join(d.get('tags', [])))}</p>"
            f"<p><a href='{html.escape(d.get('edit_link','#'))}' style='background:#0b5cad;color:#fff;padding:8px 14px;"
            f"border-radius:6px;text-decoration:none'>Edit in WordPress</a> &nbsp; "
            f"<a href='{html.escape(d.get('preview_link','#'))}'>Preview</a></p>"
            + (f"<ul style='color:#9a3412'>{warn}</ul>" if warn else "")
            + (f"<p><b>Editor notes:</b> {html.escape(d['editor_notes'])}</p>" if d.get("editor_notes") else "")
            + f"<p><b>Sources</b></p><ul>{srcs}</ul></div>"
        )
    if run.get("errors"):
        parts.append("<p style='font-family:sans-serif;color:#555'>Skipped this run:<br>"
                     + "<br>".join(html.escape(e) for e in run["errors"]) + "</p>")
    return "".join(parts)


def slack_payload(run: dict) -> dict:
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": _headline(run)}}]
    if run.get("fatal"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"*Error:* {run['fatal'][:2800]}\nNothing was drafted. Check the run log in GitHub Actions."}})
    for d in run["drafts"]:
        text = (f"*{d['headline']}*\n{_status_line(d)}\n"
                f"Categories: {', '.join(d['categories'])} · Focus: _{d.get('focus_keyword','')}_")
        if d.get("warnings"):
            text += "\n" + "\n".join(f"⚠️ {w}" for w in d["warnings"][:5])
        if d.get("editor_notes"):
            text += f"\n>{d['editor_notes'][:500]}"
        section: dict = {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
        if d.get("image_url"):
            section["accessory"] = {"type": "image", "image_url": d["image_url"], "alt_text": "featured image"}
        blocks.append(section)
        if d.get("edit_link"):
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Edit in WordPress"},
                 "url": d["edit_link"], "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "Preview"}, "url": d["preview_link"]},
            ]})
        blocks.append({"type": "divider"})
    if run.get("errors"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "Skipped: " + "; ".join(run["errors"])[:2900]}]})
    return {"text": _headline(run), "blocks": blocks[:49]}


def send_all(cfg, http, run: dict) -> list[str]:
    """Send the run summary through every configured channel. Returns channels used."""
    secrets, alerts = cfg.secrets, cfg["alerts"]
    quiet = not run["drafts"] and not run.get("fatal") and not run.get("failures")
    if quiet and not alerts.get("notify_on_empty_run"):
        return []
    sent = []
    subject = "[Sharkophile] " + _headline(run).split(" ", 1)[1]

    if alerts.get("slack") and secrets.slack_webhook_url:
        try:
            resp = http.post(secrets.slack_webhook_url, data=json.dumps(slack_payload(run)),
                             headers={"Content-Type": "application/json"})
            if resp.status_code < 300:
                sent.append("slack")
            else:
                log.error("Slack alert failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error("Slack alert failed: %s", exc)

    if alerts.get("webhook") and secrets.alert_webhook_url:
        try:
            resp = http.post(secrets.alert_webhook_url, data=json.dumps(
                {"subject": subject, "text": summary_text(run), "drafts": run["drafts"],
                 "errors": run.get("errors", []), "run_id": run.get("run_id")}),
                headers={"Content-Type": "application/json"})
            if resp.status_code < 300:
                sent.append("webhook")
        except Exception as exc:
            log.error("Webhook alert failed: %s", exc)

    if alerts.get("email") and secrets.smtp_host and secrets.editor_email:
        try:
            send_email(secrets, subject, summary_text(run), summary_html(run))
            sent.append("email")
        except Exception as exc:
            log.error("Email alert failed: %s", exc)
    return sent


def send_email(secrets, subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = secrets.smtp_from or secrets.smtp_user
    msg["To"] = ", ".join(secrets.editor_email)
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{html_body}</body></html>", subtype="html")
    ctx = ssl.create_default_context()
    if secrets.smtp_port == 465:
        with smtplib.SMTP_SSL(secrets.smtp_host, 465, context=ctx, timeout=30) as s:
            if secrets.smtp_user:
                s.login(secrets.smtp_user, secrets.smtp_password or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=30) as s:
            s.starttls(context=ctx)
            if secrets.smtp_user:
                s.login(secrets.smtp_user, secrets.smtp_password or "")
            s.send_message(msg)
