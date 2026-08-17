# durable-filesystem

A persistent filesystem for Claude on claude.ai, backed by a scoped Dropbox app folder. Gives claude.ai the auto-memory behaviour that Claude Code gets from local files.

```
SKILL.md               the skill Claude reads; command surface + memory conventions
bin/cfs.py             the CLI (stdlib only — the sandbox cannot pip install)
bin/bootstrap_auth.py  one-time OAuth flow to obtain a refresh token
user-preferences.md    text to paste into claude.ai personal preferences
package.py             builds durable-filesystem.zip for upload
test_cfs.py            offline tests (no network)
integration_test.sh    live tests against the app folder
credentials.json       app key/secret/refresh token — gitignored, and a secret
```

## Setup

1. Create a Dropbox app scoped to an **app folder**, and enable the `files.content.read`, `files.content.write` and `files.metadata.read` permissions. Remember to click **Submit** on the Permissions tab — the checkboxes silently revert otherwise, and the resulting token will authorise nothing.
2. Put the app key and secret in `credentials.json` (see `credentials.example.json`).
3. `python bin/bootstrap_auth.py` → open the printed URL, approve, copy the code.
4. `python bin/bootstrap_auth.py --code <code>` → exchanges it for a refresh token. Access tokens expire in ~4 hours, so a bare one is useless here; the skill mints them on demand from the refresh token.
5. `python package.py` → produces `durable-filesystem.zip`.
6. Upload the zip to claude.ai under Settings → Capabilities → Skills.
7. Paste `user-preferences.md` into Settings → Profile → personal preferences.
8. Confirm code-execution network egress allows `api.dropboxapi.com` and `content.dropboxapi.com`. The sandbox is network-isolated by default; without this the skill cannot reach Dropbox at all.

Note that the skill, not the Dropbox connector, must be used for all writes. The connector can see the same files but raises a permission dialog on every write; the skill has unguarded access to its own scoped folder. Both SKILL.md and the preferences text say so explicitly.

## The one design decision worth knowing

Every write to an existing file requires that file's current `rev`, obtainable only by reading it. This single mechanism enforces both guardrails at once:

- **Read-before-write**, because the rev cannot be named without a read.
- **No stale writes**, because Dropbox verifies the rev server-side (`mode=update` + `strict_conflict`) and rejects the upload on mismatch.

No local "last read" state is kept, and none would be trustworthy if it were — the sandbox is per-conversation and can reset mid-session, so anything cached locally would be unreliable exactly when it mattered. Pushing the check to the server also makes it correct across two conversations writing concurrently.

A rev is therefore not a version number but **evidence that the caller has seen the file's current bytes**. Every command is audited against that: `read`, `write`, `edit` and `upload` disclose a rev because you have either seen or authored the content; `list`, `search`, `diff`, `restore`, `read --rev` and the stale-rev error all deliberately withhold it. Disclosing one anywhere else mints the evidence for free and voids the guarantee, so there are integration tests pinning each case.

## Command surface

`list read write edit delete rename copy grep search diff history restore upload download`

The surface is curated, and that is the enforcement mechanism. There is no raw API passthrough: `/2/files/upload` is unreachable except through `write`, `edit` and `upload`, all of which refuse to build the request without a rev. Expose the primitive and the rev rule degrades from an invariant into a suggestion — one that would be bypassed exactly when a stale-rev rejection makes it inconvenient, which is when it is doing its most important work.

So: thin where there is no invariant (`copy`, `search`, `history`, `download`), deliberately thick where there is (`write`, `edit`, `delete`, `upload`). The thickness is the product. Don't "simplify" it away.

`edit` and `write` take strings as JSON on stdin rather than as shell arguments, and there is intentionally no way to read `old_str` from a file. File input would let the bytes be lifted mechanically (`sed`, `grep`) so that an edit never demonstrates knowledge of what it changes — which is the only reason matching on `old_str` exists.

`grep` fetches files and matches locally rather than using Dropbox's search index, because that index is asynchronous and cannot find a file written moments ago — precisely when a mid-conversation search is most likely.

## Security posture

The app is scoped to a single Dropbox app folder; nothing outside it is reachable. That scoping is the entire defence, because:

- **`credentials.json` ships in the zip in plaintext.** The uploaded skill is itself a credential — anyone holding it has full read/write on the app folder. Don't commit it, don't share the zip.
- **Egress allowlisting is not a strong boundary.** Published research shows the sandbox's domain allowlist can be worked around via prompt injection, so treat the app folder as the only real containment.
- **Auto-memory is a persistence sink.** Anything written to `/memory` loads into every future conversation. SKILL.md and the preferences text both instruct Claude to record only what the user established, never facts lifted from fetched pages, and to treat what it reads back as data rather than instructions.
- **Memory corruption is recoverable.** Dropbox keeps 30 days of revisions, and `history` / `diff` / `restore` expose them. This is the counterweight to auto-memory: a bad or injected write is a rollback, not a permanent fact.
- **Area roots are pinned.** `/memory` cannot be deleted or renamed, because losing it fails silently — nothing errors afterwards, memory simply stops loading. Entries inside it remain freely deletable.

## Testing

```
python test_cfs.py         # 44 offline tests, no network
bash integration_test.sh   # 108 live tests against the app folder
```

The offline suite covers path traversal, edit ambiguity, JSON payload parsing and escaping, the write-mode rules, the retry policy, and root protection. The integration suite covers every command against the real API, including the guardrails only the server can enforce:

- a stale-rev write is rejected **and verified not to have clobbered**
- every rev-disclosure path is asserted against the file's actual current rev
- shell metacharacters survive verbatim through the JSON layer
- binaries round-trip byte-identical
- a file is deliberately corrupted, then recovered with `restore`

The integration suite creates and removes `/_cfs_test`; it leaves the folder as it found it.

## Rate limits

Dropbox serialises writes per namespace and rejects concurrent ones with `too_many_write_operations`. This is handled with retry and jittered exponential backoff at the transport layer, honouring `Retry-After`. Locking would not work: the contention can come from another session or device that no local lock could see, and a 429 means the request was refused rather than partially applied, so retrying is safe.
