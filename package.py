#!/usr/bin/env python3
"""Build the uploadable skill zip.

The zip embeds credentials.json in plaintext. Treat the artefact as a secret:
anyone holding it has full read/write on the Dropbox app folder.
"""

import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "durable-filesystem"
MEMBERS = ["SKILL.md", "bin/cfs.py", "credentials.json"]


def main() -> int:
    creds_path = os.path.join(HERE, "credentials.json")
    if not os.path.exists(creds_path):
        raise SystemExit("credentials.json missing -- run bin/bootstrap_auth.py first.")
    with open(creds_path, encoding="utf-8") as fh:
        creds = json.load(fh)
    if not creds.get("refresh_token"):
        raise SystemExit(
            "credentials.json has no refresh_token -- run bin/bootstrap_auth.py "
            "to complete the OAuth flow. A bare access token expires in ~4 hours "
            "and would leave the skill dead by tomorrow."
        )

    out = os.path.join(HERE, f"{NAME}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in MEMBERS:
            src = os.path.join(HERE, member)
            if not os.path.exists(src):
                raise SystemExit(f"Missing {member}")
            zf.write(src, f"{NAME}/{member}")

    print(f"Wrote {out}")
    print("Contains a plaintext refresh token -- do not commit or share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
