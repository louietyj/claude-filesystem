# durable-filesystem

A persistent filesystem for Claude on claude.ai, backed by a scoped Dropbox app folder. Gives claude.ai the auto-memory behaviour that Claude Code gets from local files.

```
SKILL.md              the skill Claude reads; command surface + memory conventions
bin/cfs.py            the CLI (stdlib only — the sandbox cannot pip install)
bin/bootstrap_auth.py one-time OAuth flow to obtain a refresh token
user-preferences.md   text to paste into claude.ai personal preferences
package.py            builds durable-filesystem.zip for upload
test_cfs.py           offline tests (no network)
credentials.json      app key/secret/refresh token — gitignored, and a secret
```

## Setup

1. `python bin/bootstrap_auth.py` → open the printed URL, approve, copy the code.
2. `python bin/bootstrap_auth.py --code <code>` → writes the refresh token.
3. `python package.py` → produces `durable-filesystem.zip`.
4. Upload the zip to claude.ai under Settings → Capabilities → Skills.
5. Paste `user-preferences.md` into Settings → Profile → personal preferences.
6. Confirm code-execution network egress allows `api.dropboxapi.com` and `content.dropboxapi.com`. The sandbox is network-isolated by default; without this the skill cannot reach Dropbox at all.

## Command surface

`list read write edit delete rename copy grep search diff history restore upload download`

The surface is curated, and that is the enforcement mechanism. There is no raw API passthrough: `/2/files/upload` is unreachable except through `write`, `edit` and `upload`, all of which refuse to build the request without a rev. Expose the primitive and the rev rule degrades from an invariant into a suggestion — one that would be bypassed exactly when a stale-rev rejection makes it inconvenient, which is when it is doing its most important work.

So: thin where there is no invariant (`copy`, `search`, `history`, `download`), deliberately thick where there is (`write`, `edit`, `delete`, `upload`). The thickness is the product. Don't "simplify" it away.

`edit` and `write` take strings as JSON on stdin rather than as shell arguments, and there is intentionally no way to read `old_str` from a file. File input would let the bytes be lifted mechanically (`sed`, `grep`) so that an edit never demonstrates knowledge of what it changes — which is the only reason matching on `old_str` exists.

## The one design decision worth knowing

Every write to an existing file requires that file's current `rev`, obtainable only by reading it. This single mechanism enforces both guardrails at once:

A rev is therefore not a version number but *evidence that the caller has seen
the file's current bytes*. Every command is audited against that: `read`,
`write`, `edit` and `upload` disclose a rev because you have either seen or
authored the content; `list`, `search`, `diff`, `restore`, `read --rev` and the
stale-rev error all deliberately withhold it. Disclosing one anywhere else mints
the evidence for free and voids the guarantee — there are integration tests
pinning each of those.

- **Read-before-write**, because the rev cannot be named without a read.
- **No stale writes**, because Dropbox verifies the rev server-side (`mode=update` + `strict_conflict`) and rejects the upload on mismatch.

No local "last read" state is kept, and none would be trustworthy if it were — the sandbox is per-conversation and can reset mid-session, so anything cached locally would be unreliable exactly when it mattered. Pushing the check to the server also makes it correct across two conversations writing concurrently.

## Security posture

The app is scoped to a single Dropbox app folder; nothing outside it is reachable. That scoping is the entire defence, because:

- **`credentials.json` ships in the zip in plaintext.** The uploaded skill is itself a credential. Anyone holding it has full read/write on the app folder. Don't commit it, don't share the zip.
- **Egress allowlisting is not a strong boundary.** Published research shows the sandbox's domain allowlist is reachable around via prompt injection, so treat the app folder as the only real containment.
- **Auto-memory is a persistence sink.** Anything written to `/memory` loads into every future conversation. The skill and the preferences text both instruct Claude to record only what the user established, never facts lifted from fetched pages — content arriving from outside gets a pointer, not an entry.

- **Memory corruption is recoverable.** Dropbox keeps 30 days of revisions, and `history`/`restore` expose them. This is the counterweight to auto-memory: a bad or injected write is a rollback, not a permanent fact.

## Testing

```
python test_cfs.py         # 27 offline tests, no network
bash integration_test.sh   # 41 live tests against the app folder
```

The offline suite covers path traversal, edit ambiguity, JSON payload parsing and escaping, and the write-mode rules. The integration suite covers every command against the real API, including the guardrails only the server can enforce: a stale-rev write is rejected _and verified not to have clobbered_, shell metacharacters survive verbatim through the JSON layer, binaries round-trip byte-identical, and a file is deliberately corrupted and then recovered with `restore`.

The integration suite creates and removes `/_cfs_test`; it leaves the folder as it found it.
