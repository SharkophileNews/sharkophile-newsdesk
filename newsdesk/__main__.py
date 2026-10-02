"""Command line entry point.

    python -m newsdesk run            # full run: discover, write, draft in WordPress, alert
    python -m newsdesk run --dry-run  # everything except WordPress writes and alerts
    python -m newsdesk run --url URL  # write up a specific article an editor picked
    python -m newsdesk discover       # list today's candidate stories (no model calls)
    python -m newsdesk check          # verify credentials, permissions and site setup
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config
from .discover import cluster_items, discover
from .http import HttpClient


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_discover(cfg, args) -> int:
    http = HttpClient()
    items = discover(cfg, http)
    clusters = cluster_items(items)
    clusters.sort(key=lambda c: c.newest.isoformat() if c.newest else "", reverse=True)
    for c in clusters:
        outlets = ", ".join(sorted({i.publisher or i.domain for i in c.items}))
        when = c.newest.strftime("%b %d %H:%M") if c.newest else "?"
        print(f"[{when}] {c.title}  ({len(c.items)}× — {outlets})")
        if args.verbose:
            for i in c.items:
                print(f"      {i.url}")
    print(f"\n{len(clusters)} stories from {len(items)} items")
    return 0


def cmd_check(cfg, args) -> int:
    from .llm import Claude
    from .wordpress import SEO_META_KEYS, WordPress

    http = HttpClient(retries=1)
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"{'✔' if good else '✖'} {label}{(' — ' + detail) if detail else ''}")

    s = cfg.secrets
    line(bool(s.anthropic_api_key), "ANTHROPIC_API_KEY set")
    line(bool(s.openai_api_key) or cfg["images"]["provider"] == "none", "OPENAI_API_KEY set (images)")
    line(bool(s.wp_user and s.wp_app_password), "WP_USER and WP_APP_PASSWORD set")
    alerts = cfg["alerts"]
    channels = [n for n, on, good in (("Slack", alerts.get("slack"), s.slack_webhook_url),
                                       ("email", alerts.get("email"), s.smtp_host and s.editor_email),
                                       ("webhook", alerts.get("webhook"), s.alert_webhook_url)) if on and good]
    wanted = [n for n in ("slack", "email", "webhook") if alerts.get(n)]
    if wanted:
        line(bool(channels), "Editor alert channel configured", ", ".join(channels) or "none")
    else:
        print("• Editor alerts switched off in config.yaml — check Posts → Drafts in WordPress")

    wp = WordPress(cfg.site_url, s.wp_user, s.wp_app_password, http)
    try:
        me = wp.whoami()
        caps = me.get("capabilities", {})
        line(True, f"WordPress login as '{me.get('name')}'", ", ".join(me.get("roles", [])))
        for cap in ("edit_posts", "upload_files", "manage_categories"):
            line(bool(caps.get(cap)), f"Capability {cap}")
    except Exception as exc:
        line(False, "WordPress login", str(exc))
    try:
        cats = wp.categories()
        missing = [c for c in cfg["wordpress"]["allowed_categories"] if c not in cats]
        line(not missing, f"Categories found ({len(cats)})", "missing: " + ", ".join(missing) if missing else "")
    except Exception as exc:
        line(False, "Read categories", str(exc))
    try:
        meta = wp.registered_post_meta()
        plugin = cfg["site"]["seo_plugin"]
        want = set(SEO_META_KEYS.get(plugin, {}).values())
        line(want <= meta, f"SEO fields writable ({plugin})",
             "" if want <= meta else "activate the Sharkophile Newsdesk Support plugin")
        line("_newsdesk_report" in meta, "Newsdesk metadata fields registered",
             "" if "_newsdesk_report" in meta else "provided by the Sharkophile Newsdesk Support plugin")
    except Exception as exc:
        line(False, "Read post schema", str(exc))
    if s.anthropic_api_key:
        try:
            claude = Claude(s.anthropic_api_key, http)
            claude.json(cfg["models"]["triage"], "Reply with JSON.", "Say ok.",
                        {"type": "object", "properties": {"ok": {"type": "boolean"}},
                         "required": ["ok"], "additionalProperties": False}, max_tokens=50)
            line(True, "Claude API reachable")
        except Exception as exc:
            line(False, "Claude API", str(exc)[:200])
    if s.openai_api_key:
        try:
            r = http.get("https://api.openai.com/v1/models/" + cfg["images"]["model"],
                         headers={"Authorization": f"Bearer {s.openai_api_key}"})
            line(r.status_code == 200, f"OpenAI image model {cfg['images']['model']} available",
                 "" if r.status_code == 200 else f"HTTP {r.status_code}")
        except Exception as exc:
            line(False, "OpenAI API", str(exc))
    print("\nAll good." if ok else "\nFix the ✖ items above, then run again.")
    return 0 if ok else 1


def cmd_run(cfg, args) -> int:
    from .pipeline import Newsdesk

    desk = Newsdesk(cfg, dry_run=args.dry_run)
    summary = desk.run(max_drafts=args.max, urls=args.url or None)
    print(json.dumps({k: summary[k] for k in ("run_id", "considered", "errors", "usage", "alerts_sent")
                      if k in summary}, indent=2))
    for d in summary["drafts"]:
        print(f"\n■ {d['headline']}\n  {d.get('edit_link','')}")
        for w in d["warnings"]:
            print(f"  ⚠ {w}")
    print(f"\nOutputs: {desk.out}")
    return 1 if summary.get("fatal") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newsdesk", description="Sharkophile automated newsdesk")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="discover stories and create WordPress drafts")
    run.add_argument("--dry-run", action="store_true", help="write previews locally; no WordPress writes or alerts")
    run.add_argument("--max", type=int, help="max drafts this run")
    run.add_argument("--url", action="append", help="write up this article URL (repeatable)")
    sub.add_parser("discover", help="list candidate stories")
    sub.add_parser("check", help="verify setup")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    return {"run": cmd_run, "discover": cmd_discover, "check": cmd_check}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
