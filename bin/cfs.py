#!/usr/bin/env python3
"""
cfs -- a persistent filesystem for Claude, backed by a scoped Dropbox app folder.

Guardrails are enforced structurally, not by convention:

  * Every mutation of an existing file requires the file's current ``rev``.
    The rev is only obtainable by reading the file, so "you must read before
    you write" is enforced by the fact that you cannot name the rev otherwise.
  * Dropbox performs the compare-and-swap server-side (``mode=update`` +
    ``strict_conflict``), so a stale rev is rejected even if two conversations
    race. No local state is consulted, and none is trusted.
  * ``edit`` refuses ambiguous matches: old_str must appear exactly once.

Stdlib only -- the sandbox has no package installation.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"

CREDS_ENV = "CFS_CREDENTIALS"
DEFAULT_CREDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json")
TOKEN_CACHE = os.path.join(tempfile.gettempdir(), ".cfs-token.json")

MAX_VIEW_CHARS = 16000


class CfsError(Exception):
    """An error we want reported to Claude as a clean message, not a traceback."""


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def load_credentials() -> dict:
    path = os.environ.get(CREDS_ENV, DEFAULT_CREDS)
    if not os.path.exists(path):
        raise CfsError(
            f"No credentials at {path}. Expected a JSON file with app_key, "
            f"app_secret and refresh_token (see bootstrap_auth.py)."
        )
    with open(path, encoding="utf-8") as fh:
        creds = json.load(fh)
    for key in ("app_key", "app_secret", "refresh_token"):
        if not creds.get(key):
            raise CfsError(f"Credentials at {path} are missing '{key}'.")
    return creds


def access_token() -> str:
    """Mint (or reuse) a short-lived access token from the long-lived refresh token."""
    creds = load_credentials()
    # Fingerprint the refresh token so that rotating credentials -- or changing
    # the app's granted scopes, which requires re-authorising -- invalidates the
    # cache. Without this, a cached token silently outlives the grant it came
    # from and every call fails with a missing_scope that the credentials on
    # disk do not explain.
    fingerprint = hashlib.sha256(creds["refresh_token"].encode()).hexdigest()[:16]

    try:
        with open(TOKEN_CACHE, encoding="utf-8") as fh:
            cached = json.load(fh)
        if (
            cached.get("fingerprint") == fingerprint
            and cached.get("expires_at", 0) > time.time() + 60
        ):
            return cached["access_token"]
    except (OSError, ValueError, KeyError):
        pass

    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}
    ).encode()
    basic = base64.b64encode(
        f"{creds['app_key']}:{creds['app_secret']}".encode()
    ).decode()
    req = urllib.request.Request(
        f"{API}/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise CfsError(f"Could not refresh access token ({exc.code}): {detail}") from exc

    token = payload["access_token"]
    try:
        with open(TOKEN_CACHE, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "access_token": token,
                    "expires_at": time.time() + payload.get("expires_in", 14400),
                    "fingerprint": fingerprint,
                },
                fh,
            )
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass  # cache is an optimisation; failing to write it is not fatal
    return token


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def rpc(endpoint: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{endpoint}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        },
    )
    return _send(req)


def content_upload(payload: dict, data: bytes) -> dict:
    req = urllib.request.Request(
        f"{CONTENT}/2/files/upload",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": _api_arg(payload),
        },
    )
    return _send(req)


def content_download(payload: dict) -> tuple[bytes, dict]:
    req = urllib.request.Request(
        f"{CONTENT}/2/files/download",
        data=b"",
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Dropbox-API-Arg": _api_arg(payload),
        },
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), json.loads(resp.headers["Dropbox-API-Result"])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            delay = _retry_after(exc, body, attempt)
            if delay is None:
                raise _translate(exc, body) from exc
            time.sleep(delay)
    raise CfsError("Unreachable")  # pragma: no cover


def _api_arg(payload: dict) -> str:
    """Dropbox-API-Arg must be HTTP-header-safe: escape non-ASCII."""
    return json.dumps(payload, ensure_ascii=True)


MAX_RETRIES = 5


def _retry_after(exc: urllib.error.HTTPError, body: str, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if this is not retryable.

    Dropbox serialises writes per namespace and rejects concurrent ones with
    too_many_write_operations. The request is refused, not partially applied, so
    retrying is safe -- and it is the only fix that works, since the contention
    can come from another session or device that no local lock could see.
    """
    retryable = exc.code in (429, 503) or "too_many_" in body or "rate_limit" in body
    if not retryable or attempt >= MAX_RETRIES:
        return None

    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    # Dropbox reports retry_after at the top level on some responses and inside
    # the error object on others; accept either rather than betting on one.
    try:
        parsed = json.loads(body)
        for hint in (parsed.get("retry_after"), parsed.get("error", {}).get("retry_after")):
            if hint is not None:
                return min(float(hint), 30.0)
    except (ValueError, AttributeError, TypeError):
        pass
    # Exponential backoff with jitter, so parallel callers do not resynchronise
    # onto the same retry instant and collide again.
    return min(2.0**attempt, 16.0) * (0.5 + random.random())


def _send(req: urllib.request.Request) -> dict:
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            delay = _retry_after(exc, body, attempt)
            if delay is None:
                raise _translate(exc, body) from exc
            time.sleep(delay)
    raise CfsError("Unreachable")  # pragma: no cover


def _translate(exc: urllib.error.HTTPError, body: str) -> CfsError:
    """Turn a Dropbox error body into something actionable."""
    try:
        tag = json.loads(body).get("error", {})
    except ValueError:
        tag = {}
    summary = json.dumps(tag) if tag else body.strip()

    if "conflict" in summary:
        return CfsError(
            "Write rejected: the file changed since you read it (rev is stale). "
            "Re-read the file, re-apply your change to the current content, and retry.\n"
            f"Dropbox said: {summary}"
        )
    if "not_found" in summary:
        return CfsError(f"Path does not exist.\nDropbox said: {summary}")
    if exc.code == 401:
        return CfsError(f"Dropbox rejected the credentials (401): {summary}")
    return CfsError(f"Dropbox API error {exc.code}: {summary}")


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def normalise(path: str) -> str:
    """Reject traversal and normalise to a Dropbox-style absolute path.

    The app folder is already the security boundary; this is defence in depth
    plus a guard against typos that would silently address the wrong file.
    """
    if not path.startswith("/"):
        path = "/" + path
    path = urllib.parse.unquote(path)
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise CfsError(f"Refusing path containing '..': {path}")
        parts.append(part)
    return "/" + "/".join(parts)


def api_path(path: str) -> str:
    """Dropbox names the app-folder root as the empty string, not '/'."""
    norm = normalise(path)
    return "" if norm == "/" else norm


# --------------------------------------------------------------------------
# argument values: literal, @file, or - for stdin
# --------------------------------------------------------------------------


def read_payload(required: tuple[str, ...], example: str) -> dict:
    """Read a JSON object from stdin.

    Strings arrive as JSON rather than as shell arguments deliberately. It keeps
    multi-line content free of shell quoting hazards, and -- for `edit` -- it
    means old_str must be produced inline in the command. There is intentionally
    no way to read old_str from a file: that would let the bytes be extracted
    mechanically (sed, grep) so that the edit never demonstrates knowledge of
    what it is changing, which is the entire point of matching on old_str.
    """
    if sys.stdin.isatty():
        raise CfsError(
            "This command expects a JSON object on stdin. Use a quoted heredoc so "
            f"the shell does not interpret the content:\n\n{example}"
        )
    raw = sys.stdin.read()
    if not raw.strip():
        raise CfsError(f"Empty stdin; expected a JSON object.\n\n{example}")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise CfsError(
            f"Could not parse stdin as JSON: {exc}. Newlines inside strings must be "
            f"escaped as \\n.\n\n{example}"
        ) from exc
    if not isinstance(payload, dict):
        raise CfsError(f"Expected a JSON object, got {type(payload).__name__}.")
    missing = [key for key in required if key not in payload]
    if missing:
        raise CfsError(f"Missing key(s) in JSON payload: {', '.join(missing)}.\n\n{example}")
    for key in required:
        if not isinstance(payload[key], str):
            raise CfsError(f"'{key}' must be a string.")
    return payload


EDIT_EXAMPLE = """\
  cfs edit /memory/notes.md --rev 0165932a <<'JSON'
  {"old_str": "- Hotel: unbooked", "new_str": "- Hotel: booked 3 Mar"}
  JSON"""

WRITE_EXAMPLE = """\
  cfs write /memory/notes.md --new <<'JSON'
  {"content": "# Notes\\n\\nFirst line.\\n"}
  JSON"""


def human_size(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.0f}B"


def numbered(text: str, start: int = 1) -> str:
    lines = text.split("\n")
    return "\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, start))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_list(args) -> str:
    entries: list[dict] = []
    payload = {"path": api_path(args.path), "recursive": args.recursive}
    try:
        result = rpc("/2/files/list_folder", payload)
    except CfsError as exc:
        if "not_found" in str(exc):
            raise CfsError(f"The path {normalise(args.path)} does not exist.") from exc
        raise
    entries.extend(result["entries"])
    while result.get("has_more"):
        result = rpc("/2/files/list_folder/continue", {"cursor": result["cursor"]})
        entries.extend(result["entries"])

    root = normalise(args.path)
    depth_of_root = root.rstrip("/").count("/")
    kept = []
    for entry in entries:
        rel_depth = entry["path_display"].count("/") - depth_of_root
        if rel_depth <= args.depth:
            kept.append(entry)

    kept.sort(key=lambda e: e["path_display"].lower())
    lines = [f"Contents of {root} (depth {args.depth}):"]
    if not kept:
        lines.append("  (empty)")
    for entry in kept:
        if entry[".tag"] == "folder":
            lines.append(f"  {'dir':>7}\t{entry['path_display']}/")
        else:
            # No rev here on purpose: read is the only way to obtain one, which
            # is what makes the rev evidence that the file has been read.
            lines.append(f"  {human_size(entry['size']):>7}\t{entry['path_display']}")
    return "\n".join(lines)


def cmd_read(args) -> str:
    """Content first, rev last -- on purpose.

    A rev only means anything if it is evidence you saw the bytes it describes.
    Printing it up front lets `read ... | head -3` harvest a valid-looking rev
    while discarding the content it is supposed to certify -- the exactness
    check on write then protects against clobbering bytes you looked at, but
    not against an edit built on a stale memory of parts of the file that never
    reached you. Putting the rev after the content does not stop a determined
    `tail`, but it does mean the reflexive `head` shortcut yields no rev at all.
    """
    path = normalise(args.path)
    if args.rev:
        data, meta = content_download({"path": f"rev:{args.rev}"})
    else:
        data, meta = content_download({"path": api_path(path)})
    text = data.decode("utf-8", "replace")
    # A single trailing newline terminates the last line; it does not start a
    # further, empty one. Without stripping it, split("\n") reports one line
    # too many on almost every file we write, which would make the "read all
    # N lines" warning below wrong on the common case rather than the edge case.
    display_text = text[:-1] if text.endswith("\n") else text
    all_lines = display_text.split("\n")
    total = len(all_lines)

    top = f"{path}  ({total} line(s))\n"

    # An unambiguous marker, not "---": memory files are markdown, where "---"
    # is legitimately YAML frontmatter or a horizontal rule. A separator that
    # can appear in real content is a separator that can mislabel where the
    # content actually ends.
    MARK = "[end of file content]"

    if args.rev:
        # Deliberately never discloses the current rev: this call proves you
        # have seen an old version, not the live one, so it must not license a
        # write.
        footer = (
            f"\n{MARK}\nThis is a historical revision (rev {args.rev}), not the "
            f"current file. It cannot be used to write -- read {path} without "
            "--rev for that, or use restore."
        )
    else:
        footer = (
            f"\n{MARK}\nrev: {meta['rev']}   (pass this rev to edit/write/delete -- "
            f"only valid if you read all {total} line(s) above, not a piped "
            "excerpt)"
        )

    if args.lines:
        try:
            start_s, end_s = args.lines.split("-", 1)
            start, end = int(start_s), int(end_s)
        except ValueError as exc:
            raise CfsError("--lines expects START-END, e.g. 1-40 or 20--1") from exc
        if end == -1:
            end = total
        selected = all_lines[max(start - 1, 0) : end]
        body = numbered("\n".join(selected), start=max(start, 1))
        if not args.rev:
            footer += (
                f"\nThis rev covers the WHOLE file, not just lines {start}-{end}. "
                "A partial view is fine for reading, but do not edit content "
                "outside the range you actually saw."
            )
        return top + body + footer

    if len(display_text) > MAX_VIEW_CHARS and not args.full:
        shown = display_text[:MAX_VIEW_CHARS]
        truncated_at = numbered(shown).count("\n") + 1
        footer += (
            f"\n[truncated after {truncated_at} of {total} lines ({MAX_VIEW_CHARS} "
            "chars); the rev above still describes the WHOLE file. Use --lines "
            "START-END to page through the rest, or --full, before editing "
            "anything past what you have actually read.]"
        )
        return top + numbered(shown) + footer

    return top + numbered(display_text) + footer


MEMORY_ROOT = "/memory"
PROTECTED = {MEMORY_ROOT}


def guard_protected(path: str, verb: str) -> None:
    """Refuse to destroy an area root.

    Losing /memory is a silent failure: nothing errors afterwards, memory simply
    stops loading and every later conversation starts blank. Individual entries
    are still freely deletable -- only the root is pinned.
    """
    if normalise(path) in PROTECTED:
        raise CfsError(
            f"Refusing to {verb} {normalise(path)}: it is the root of an area that "
            "other sessions rely on, and losing it fails silently rather than "
            "loudly. Remove entries inside it individually, or do this from the "
            "Dropbox UI if you really mean it."
        )


def upload_bytes(path: str, data: bytes, args) -> dict:
    """Upload with the right write mode, and diagnose an 'add' conflict correctly.

    Dropbox reports both failures as a 'conflict': a stale rev under mode=update,
    and an existing path under mode=add. They need opposite advice -- re-read
    versus pick another path -- so they must not share an error message.
    """
    mode = write_mode(args, path)
    try:
        return content_upload(
            {
                "path": api_path(path),
                "mode": mode,
                "autorename": False,
                "strict_conflict": True,
                "mute": True,
            },
            data,
        )
    except CfsError as exc:
        if args.new and "conflict" in str(exc):
            raise CfsError(
                f"{path} already exists, so --new refused to create it. Nothing was "
                "written. Read the file and pass --rev <rev> if you meant to "
                "overwrite it, or choose a different path."
            ) from exc
        raise


def write_mode(args, path: str):
    """Shared by write and upload: 'add' for new files, CAS update otherwise."""
    if args.new:
        if args.rev:
            raise CfsError("--new and --rev are mutually exclusive.")
        return "add"
    if not args.rev:
        raise CfsError(
            f"Refusing to overwrite {path} without --rev. Read the file first and "
            "pass the rev it reports, or use --new if you intend to create a new file."
        )
    return {".tag": "update", "update": args.rev}


def cmd_write(args) -> str:
    path = normalise(args.path)
    if args.content is not None:
        content = args.content
    else:
        content = read_payload(("content",), WRITE_EXAMPLE)["content"]
    data = content.encode("utf-8")

    meta = upload_bytes(path, data, args)
    verb = "Created" if args.new else "Wrote"
    return f"{verb} {path} ({len(data)} bytes).\nnew rev: {meta['rev']}"


def cmd_edit(args) -> str:
    path = normalise(args.path)
    payload = read_payload(("old_str", "new_str"), EDIT_EXAMPLE)
    old, new = payload["old_str"], payload["new_str"]
    if not args.rev:
        raise CfsError(
            f"Refusing to edit {path} without --rev. Read the file first and pass "
            "the rev it reports."
        )
    if old == new:
        raise CfsError("--old and --new are identical; nothing to do.")

    data, meta = content_download({"path": api_path(path)})
    text = data.decode("utf-8", "replace")

    if meta["rev"] != args.rev:
        # Deliberately does not disclose the current rev. Handing it over here
        # would mint a proof-of-read token without a read, letting the retry
        # reapply an edit computed against content nobody has looked at -- which
        # is the exact situation the rev is there to prevent.
        raise CfsError(
            f"Stale rev: {path} has changed since you read it. Re-read the file, "
            "re-apply your change to the content you get back, and retry with the "
            "rev from that read."
        )

    updated = apply_replacement(text, old, new, path, replace_all=args.all)
    if updated == text:
        raise CfsError("Replacement produced no change; nothing written.")
    result = content_upload(
        {
            "path": api_path(path),
            "mode": {".tag": "update", "update": args.rev},
            "autorename": False,
            "strict_conflict": True,
            "mute": True,
        },
        updated.encode("utf-8"),
    )
    return f"Edited {path}.\nnew rev: {result['rev']}"


def _diagnose_no_match(text: str, old: str) -> str:
    """Explain *why* old_str missed, when the reason is boring.

    A failed match is usually a stray trailing space or an indentation
    difference, not a misremembered line. Saying which turns a retry-and-hope
    loop into a single corrected edit.
    """
    def strip_trailing(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.split("\n"))

    def collapse(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    if strip_trailing(old) in strip_trailing(text):
        return (
            "\n\nThe text matches once trailing whitespace is ignored, so the file "
            "has trailing spaces your old_str does not (or vice versa). Re-read the "
            "file and copy the line exactly."
        )
    if collapse(old) and collapse(old) in collapse(text):
        return (
            "\n\nThe text matches once all whitespace is normalised, so the "
            "difference is indentation or line breaks. Re-read the file and copy "
            "the exact bytes, including leading whitespace."
        )

    first = old.split("\n", 1)[0].strip()
    if first:
        lines = text.split("\n")
        # Match on stripped lines so indentation cannot hide an obvious suggestion,
        # but report the original line so the exact bytes are visible.
        stripped = [line.strip() for line in lines]
        close = difflib.get_close_matches(first, stripped, n=1, cutoff=0.7)
        if close:
            idx = stripped.index(close[0])
            return (
                f"\n\nThe closest line in the file is line {idx + 1}:\n"
                f"  {lines[idx]!r}\nagainst your first line:\n  {first!r}"
            )
    return ""


def apply_replacement(
    text: str, old: str, new: str, path: str, replace_all: bool = False
) -> str:
    """Replace old with new, refusing anything but an unambiguous single match.

    replace_all lifts the uniqueness requirement, which is safe only because the
    caller has asked for every occurrence explicitly.
    """
    count = text.count(old)
    if count == 0:
        raise CfsError(
            f"No replacement performed: old_str did not appear verbatim in {path}."
            + _diagnose_no_match(text, old)
        )
    if replace_all:
        return text.replace(old, new)
    if count > 1:
        positions = []
        offset = 0
        for _ in range(count):
            offset = text.index(old, offset)
            positions.append(text.count("\n", 0, offset) + 1)
            offset += 1
        raise CfsError(
            f"No replacement performed: old_str appears {count} times in {path} "
            f"(lines {', '.join(map(str, positions))}). Include more surrounding "
            "context to make it unique, or pass --all to replace every occurrence."
        )
    return text.replace(old, new, 1)


def cmd_delete(args) -> str:
    path = normalise(args.path)
    if path == "/":
        raise CfsError("Refusing to delete the filesystem root.")
    guard_protected(path, "delete")
    payload: dict = {"path": api_path(path)}
    if args.rev:
        payload["parent_rev"] = args.rev
    elif not args.force:
        raise CfsError(
            f"Refusing to delete {path} without --rev (read it first) or --force "
            "(required for directories, which have no rev)."
        )
    rpc("/2/files/delete_v2", payload)
    return f"Deleted {path}"


def cmd_rename(args) -> str:
    old, new = normalise(args.old_path), normalise(args.new_path)
    if old == "/" or new == "/":
        raise CfsError("Refusing to rename the filesystem root.")
    guard_protected(old, "rename")
    rpc(
        "/2/files/move_v2",
        {"from_path": api_path(old), "to_path": api_path(new), "autorename": False},
    )
    return f"Renamed {old} -> {new}"


def cmd_copy(args) -> str:
    src, dst = normalise(args.src), normalise(args.dst)
    rpc(
        "/2/files/copy_v2",
        {"from_path": api_path(src), "to_path": api_path(dst), "autorename": False},
    )
    return f"Copied {src} -> {dst}"


def cmd_search(args) -> str:
    options: dict = {"max_results": args.max, "filename_only": args.names_only}
    if args.path:
        options["path"] = api_path(args.path)
    result = rpc("/2/files/search_v2", {"query": args.query, "options": options})

    matches = result.get("matches", [])
    if not matches:
        return (
            f"No matches for {args.query!r}. Note that Dropbox indexes content "
            "asynchronously, so a file written moments ago may not be searchable yet."
        )

    lines = [f"{len(matches)} match(es) for {args.query!r}:"]
    for match in matches:
        meta = match.get("metadata", {}).get("metadata", {})
        if meta.get("path_display"):
            lines.append(f"  {meta['path_display']}")  # no rev; see cmd_list
    if result.get("has_more"):
        lines.append("  ... more results truncated; narrow the query or raise --max.")
    return "\n".join(lines)


BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".mp3", ".mp4", ".mov", ".wav", ".bin", ".woff", ".woff2",
}


def cmd_grep(args) -> str:
    """Regex search by fetching files and matching locally.

    Dropbox's own search is full-text but has no regex, no context lines, and
    indexes asynchronously -- so it cannot find a file written moments ago,
    which is exactly when a search mid-conversation is most likely. Fetching and
    matching locally is exact and immediate; the tree is small enough that the
    cost is worth the correctness.
    """
    try:
        pattern = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as exc:
        raise CfsError(f"Invalid regular expression: {exc}")

    root = normalise(args.path)
    result = rpc("/2/files/list_folder", {"path": api_path(root), "recursive": True})
    entries = list(result["entries"])
    while result.get("has_more"):
        result = rpc("/2/files/list_folder/continue", {"cursor": result["cursor"]})
        entries.extend(result["entries"])

    files = [
        e
        for e in entries
        if e[".tag"] == "file"
        and os.path.splitext(e["path_display"])[1].lower() not in BINARY_SUFFIXES
    ]
    files.sort(key=lambda e: e["path_display"].lower())

    truncated = len(files) > args.max_files
    files = files[: args.max_files]

    lines: list[str] = []
    matched_files = 0
    for entry in files:
        try:
            data, _ = content_download({"path": api_path(entry["path_display"])})
            text = data.decode("utf-8")
        except (CfsError, UnicodeDecodeError):
            continue  # unreadable or not text; skip rather than abort the search

        hits = [
            (i, line)
            for i, line in enumerate(text.split("\n"), 1)
            if pattern.search(line)
        ]
        if not hits:
            continue
        matched_files += 1
        if args.files_only:
            lines.append(entry["path_display"])
            continue

        body = text.split("\n")
        shown: set[int] = set()
        lines.append(f"{entry['path_display']}:")
        for num, line in hits:
            lo = max(1, num - args.context)
            hi = min(len(body), num + args.context)
            for n in range(lo, hi + 1):
                if n in shown:
                    continue
                shown.add(n)
                marker = ":" if n == num else "-"
                lines.append(f"  {n}{marker} {body[n - 1]}")
        lines.append("")

    if not matched_files:
        return f"No matches for {args.pattern!r} under {root}."

    header = f"{matched_files} file(s) matched {args.pattern!r}:"
    if truncated:
        header += f" (searched the first {args.max_files} files only)"
    return header + "\n" + "\n".join(lines).rstrip()


# Deliberately tight: a diff earns its place only when it is small enough to
# eyeball at a glance. Anything bigger is clearer as the file itself, so that is
# what gets returned -- an ugly three-page diff is worse than no diff at all.
DIFF_MAX_CHANGED_LINES = 20
DIFF_MAX_CHANGED_FRACTION = 0.05


def cmd_diff(args) -> str:
    """Show what changed between two revisions, unless that would be noise.

    A diff of a mostly-rewritten file is worse than useless: pages of -/+ that
    obscure rather than explain. Past a threshold this refuses and tells you to
    read the file, which is the answer you actually wanted.
    """
    path = normalise(args.path)

    if args.rev:
        old_rev = args.rev
    else:
        result = rpc(
            "/2/files/list_revisions", {"path": api_path(path), "mode": "path", "limit": 2}
        )
        entries = result.get("entries", [])
        if len(entries) < 2:
            return f"{path} has only one revision; there is nothing to compare against."
        old_rev = entries[1]["rev"]

    old_data, old_meta = content_download({"path": f"rev:{old_rev}"})
    if args.to:
        new_data, new_meta = content_download({"path": f"rev:{args.to}"})
    else:
        new_data, new_meta = content_download({"path": api_path(path)})

    try:
        old_text = old_data.decode("utf-8")
        new_text = new_data.decode("utf-8")
    except UnicodeDecodeError:
        return f"{path} is not text at one of these revisions; cannot diff."

    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")

    if old_text == new_text:
        return f"No difference between {old_rev} and {new_meta['rev']}."

    diff = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"{path}@{old_rev}",
            tofile=f"{path}@{new_meta['rev']}", lineterm="", n=args.context,
        )
    )
    changed = sum(
        1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )
    largest = max(len(old_lines), len(new_lines), 1)

    eyeballable = (
        changed <= DIFF_MAX_CHANGED_LINES
        and changed / largest <= DIFF_MAX_CHANGED_FRACTION
    )
    if not eyeballable and not args.force:
        shown = new_text
        note = ""
        if len(shown) > MAX_VIEW_CHARS:
            shown = shown[:MAX_VIEW_CHARS]
            note = f"\n\n[truncated at {MAX_VIEW_CHARS} chars; read with --lines to page]"
        return (
            f"{changed} of ~{largest} lines differ between {old_rev} and "
            f"{new_meta['rev']} -- too much to read as a diff, so here is the "
            f"current file instead. (--force for the raw diff; "
            f"read {path} --rev {old_rev} for the older version.)\n\n"
            + numbered(shown)
            + note
        )

    header = (
        f"{changed} changed line(s) between {old_rev} "
        f"({old_meta.get('server_modified', '?')}) and {new_meta['rev']}:"
    )
    return header + "\n" + "\n".join(diff)


def cmd_history(args) -> str:
    path = normalise(args.path)
    result = rpc(
        "/2/files/list_revisions",
        {"path": api_path(path), "mode": "path", "limit": args.limit},
    )
    entries = result.get("entries", [])
    if not entries:
        return f"No revision history for {path}."

    lines = [f"Revisions of {path} (newest first):"]
    if result.get("is_deleted"):
        lines.append("  (the file is currently deleted; restore brings it back)")
    for entry in entries:
        lines.append(
            f"  {entry['server_modified']}\t{human_size(entry['size']):>7}\t"
            f"rev:{entry['rev']}"
        )
    lines.append("")
    lines.append(f"Restore one with: cfs restore {path} --rev <rev>")
    return "\n".join(lines)


def cmd_restore(args) -> str:
    path = normalise(args.path)
    rpc("/2/files/restore", {"path": api_path(path), "rev": args.rev})
    # The new rev is withheld: restoring brings back bytes the caller has not
    # necessarily seen, so it is not evidence of knowing the file's contents.
    # Read it before writing -- which you want to do anyway, to check the
    # rollback landed where you expected.
    return (
        f"Restored {path} to rev {args.rev}. Restoring adds a new revision rather "
        "than erasing anything, so this is itself reversible.\n"
        f"Read {path} to see the restored content and get its current rev."
    )


MAX_SIMPLE_UPLOAD = 150 * 1024 * 1024


def cmd_upload(args) -> str:
    path = normalise(args.path)
    try:
        with open(args.source, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise CfsError(f"Could not read local file '{args.source}': {exc.strerror}") from exc

    if len(data) > MAX_SIMPLE_UPLOAD:
        raise CfsError(
            f"'{args.source}' is {human_size(len(data))}, over the {human_size(MAX_SIMPLE_UPLOAD)} "
            "single-request limit. Chunked upload is not implemented."
        )

    meta = upload_bytes(path, data, args)
    return f"Uploaded {args.source} -> {path} ({human_size(len(data))}).\nnew rev: {meta['rev']}"


def cmd_download(args) -> str:
    path = normalise(args.path)
    data, meta = content_download({"path": api_path(path)})
    try:
        with open(args.dest, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise CfsError(f"Could not write local file '{args.dest}': {exc.strerror}") from exc
    return f"Downloaded {path} -> {args.dest} ({human_size(len(data))}).\nrev: {meta['rev']}"


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "edit and write take their strings as a JSON object on stdin, fed by a "
        "quoted heredoc:\n\n" + EDIT_EXAMPLE + "\n\nNewlines inside JSON strings "
        "are escaped as \\n. There is deliberately no way to read old_str from a "
        "file."
    )
    parser = argparse.ArgumentParser(
        prog="cfs",
        description=__doc__,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list a directory")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--recursive", action="store_true", default=True)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", help="read a file (reports the rev you need to write)")
    p.add_argument("path")
    p.add_argument("--rev", help="read a historical revision (cannot be used to write)")
    p.add_argument("--lines", help="START-END, 1-indexed; END may be -1")
    p.add_argument("--full", action="store_true", help="do not truncate long files")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser(
        "write", help='create or overwrite a file; JSON stdin {"content": "..."}'
    )
    p.add_argument("path")
    p.add_argument("--content", help="inline content, for short single-line values")
    p.add_argument("--rev", help="current rev; required when overwriting")
    p.add_argument("--new", action="store_true", help="create; fails if path exists")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser(
        "edit",
        help='replace a unique string; JSON stdin {"old_str": "...", "new_str": "..."}',
    )
    p.add_argument("path")
    p.add_argument("--rev", help="current rev, from read")
    p.add_argument(
        "--all",
        action="store_true",
        help="replace every occurrence instead of requiring a unique match",
    )
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("delete", help="delete a file or directory")
    p.add_argument("path")
    p.add_argument("--rev")
    p.add_argument("--force", action="store_true", help="allow deleting without a rev")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("rename", help="rename or move a file or directory")
    p.add_argument("old_path")
    p.add_argument("new_path")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("copy", help="copy a file or directory")
    p.add_argument("src")
    p.add_argument("dst")
    p.set_defaults(func=cmd_copy)

    p = sub.add_parser("search", help="full-text search across files")
    p.add_argument("query")
    p.add_argument("--path", help="restrict to a subtree")
    p.add_argument("--max", type=int, default=20)
    p.add_argument(
        "--names-only", action="store_true", help="match filenames rather than content"
    )
    p.set_defaults(func=cmd_search)

    p = sub.add_parser(
        "grep", help="regex search across file contents (exact, immediate)"
    )
    p.add_argument("pattern")
    p.add_argument("--path", default="/", help="subtree to search")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("-C", "--context", type=int, default=0, help="lines of context")
    p.add_argument("-l", "--files-only", action="store_true", help="paths only")
    p.add_argument("--max-files", type=int, default=200)
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("diff", help="show what changed between two revisions")
    p.add_argument("path")
    p.add_argument("--rev", help="older rev (default: the previous revision)")
    p.add_argument("--to", help="newer rev (default: current)")
    p.add_argument("-C", "--context", type=int, default=3)
    p.add_argument(
        "--force", action="store_true", help="show the diff even if it is mostly noise"
    )
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("history", help="list previous revisions of a file")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("restore", help="roll a file back to an earlier revision")
    p.add_argument("path")
    p.add_argument("--rev", required=True, help="target rev, from history")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("upload", help="upload a local file (binaries, generated artefacts)")
    p.add_argument("path")
    p.add_argument("--from", dest="source", required=True, help="local file to upload")
    p.add_argument("--rev", help="current rev; required when overwriting")
    p.add_argument("--new", action="store_true", help="create; fails if path exists")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("download", help="download to a local file")
    p.add_argument("path")
    p.add_argument("--to", dest="dest", required=True, help="local destination")
    p.set_defaults(func=cmd_download)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(args.func(args))
    except CfsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"Error: could not reach Dropbox ({exc.reason}). If this is the claude.ai "
            "sandbox, api.dropboxapi.com and content.dropboxapi.com must be allowed "
            "by the code-execution network egress setting.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
