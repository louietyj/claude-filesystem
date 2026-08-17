#!/usr/bin/env python3
"""
One-time setup: exchange a Dropbox authorisation code for a long-lived refresh token.

Dropbox access tokens expire after ~4 hours, so the skill cannot bake one in.
Instead it bakes in a refresh token and mints access tokens on demand. Getting
that refresh token needs a browser round-trip, which only a human can do.

    1. python bootstrap_auth.py            -> prints the URL to open
    2. approve in the browser, copy the code Dropbox shows
    3. python bootstrap_auth.py --code XXX -> writes credentials.json

Run this once. The resulting credentials.json is a credential in its own right:
anyone holding it has full read/write on the app folder.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS = os.path.join(HERE, "credentials.json")


def load_app() -> tuple[str, str]:
    if not os.path.exists(CREDS):
        raise SystemExit(
            f"Missing {CREDS}. Create it with:\n"
            '  {"app_key": "...", "app_secret": "..."}'
        )
    with open(CREDS, encoding="utf-8") as fh:
        creds = json.load(fh)
    if not creds.get("app_key") or not creds.get("app_secret"):
        raise SystemExit(f"{CREDS} needs both app_key and app_secret.")
    return creds["app_key"], creds["app_secret"]


def authorize_url(app_key: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": app_key,
            "response_type": "code",
            "token_access_type": "offline",
        }
    )
    return f"https://www.dropbox.com/oauth2/authorize?{query}"


def exchange(app_key: str, app_secret: str, code: str) -> dict:
    body = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code}
    ).encode()
    basic = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.dropboxapi.com/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Token exchange failed ({exc.code}): {exc.read().decode('utf-8', 'replace')}\n"
            "Authorisation codes are single-use and short-lived -- if you already "
            "tried once, get a fresh code from the URL above."
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="the code Dropbox showed after you approved")
    args = parser.parse_args()

    app_key, app_secret = load_app()

    if not args.code:
        print("Open this URL, approve, then re-run with --code <the code shown>:\n")
        print(f"  {authorize_url(app_key)}\n")
        return 0

    payload = exchange(app_key, app_secret, args.code.strip())
    if "refresh_token" not in payload:
        raise SystemExit(
            "Dropbox did not return a refresh_token. The authorize URL must include "
            f"token_access_type=offline. Response: {json.dumps(payload)}"
        )

    with open(CREDS, encoding="utf-8") as fh:
        creds = json.load(fh)
    creds["refresh_token"] = payload["refresh_token"]
    creds["account_id"] = payload.get("account_id", "")
    creds["scope"] = payload.get("scope", "")
    with open(CREDS, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
    try:
        os.chmod(CREDS, 0o600)
    except OSError:
        pass

    print(f"Wrote refresh_token to {CREDS}")
    print(f"Granted scopes: {payload.get('scope', '(none reported)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
