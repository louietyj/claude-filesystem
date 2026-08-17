#!/usr/bin/env bash
# Live integration test against the Dropbox app folder.
# Exercises every command plus the guardrails only the server can enforce.
# Cleans up after itself; leaves the folder as it found it.

export MSYS_NO_PATHCONV=1
CFS="python bin/cfs.py"
ROOT="/_cfs_test"
pass=0; fail=0

ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }

expect_ok()  { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
expect_err() {
  local d="$1" want="$2"; shift 2
  local out; out=$("$@" 2>&1)
  if [ $? -eq 0 ]; then bad "$d (succeeded, expected failure)"
  elif echo "$out" | grep -qi "$want"; then ok "$d"
  else bad "$d (wrong error: $out)"; fi
}
# expect_ok_json <desc> <json> <command...>
expect_ok_json() {
  local d="$1" js="$2"; shift 2
  if echo "$js" | "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi
}
expect_err_json() {
  local d="$1" want="$2" js="$3"; shift 3
  local out; out=$(echo "$js" | "$@" 2>&1)
  if [ $? -eq 0 ]; then bad "$d (succeeded, expected failure)"
  elif echo "$out" | grep -qi "$want"; then ok "$d"
  else bad "$d (wrong error: $out)"; fi
}
revof() { $CFS read "$1" 2>/dev/null | sed -n 's/^rev: \([a-z0-9]*\).*/\1/p'; }
# Second-newest rev, i.e. the newest one history is still willing to print.
prev_rev() { $CFS history "$1" 2>/dev/null | sed -n 's/.*rev:\([a-z0-9]*\).*/\1/p' | head -1; }
# Strip the one-line header and everything from the "[end of file content]"
# marker onward (the rev footer), so comparisons test content only -- the rev
# necessarily changes across a restore, and the header's line count would too.
body() {
  $CFS read "$1" 2>/dev/null \
    | tail -n +2 \
    | sed '/^\[end of file content\]$/,$d' \
    | sed 's/^ *[0-9]*\t//'
}

echo "=== create / write via JSON stdin ==="
expect_ok_json "write --new with JSON stdin" '{"content": "line one\nline two\n"}' \
  $CFS write $ROOT/a.md --new
body $ROOT/a.md | grep -q "line two" && ok "multi-line content round-trips" \
  || bad "multi-line content round-trips"
expect_err_json "write --new on existing path" "already exists" '{"content":"x"}' \
  $CFS write $ROOT/a.md --new
expect_err_json "overwrite without --rev" "without --rev" '{"content":"x"}' \
  $CFS write $ROOT/a.md
expect_ok "write --content inline for short values" \
  $CFS write $ROOT/short.md --content "brief" --new

echo "=== JSON payload validation ==="
expect_err_json "malformed JSON rejected" "parse stdin as JSON" '{"content": "oops' \
  $CFS write $ROOT/b.md --new
expect_err_json "missing key rejected" "new_str" '{"old_str":"a"}' \
  $CFS edit $ROOT/a.md --rev deadbeef
expect_err "no --old/--new flags on edit" "unrecognized arguments" \
  $CFS edit $ROOT/a.md --rev deadbeef --old a --new b

echo "=== read ==="
REV=$(revof $ROOT/a.md)
[ -n "$REV" ] && ok "read reports a rev ($REV)" || bad "read reports a rev"

echo "=== edit ==="
expect_err_json "edit with wrong rev" "stale" '{"old_str":"line one","new_str":"z"}' \
  $CFS edit $ROOT/a.md --rev 0123456789
expect_err_json "edit with absent old_str" "verbatim" '{"old_str":"nope","new_str":"z"}' \
  $CFS edit $ROOT/a.md --rev "$REV"
expect_ok_json "edit with correct rev" '{"old_str":"line one","new_str":"LINE ONE"}' \
  $CFS edit $ROOT/a.md --rev "$REV"
body $ROOT/a.md | grep -q "LINE ONE" && ok "edit applied" || bad "edit applied"

echo "=== escaping through the shell ==="
REV=$(revof $ROOT/a.md)
expect_ok_json "content with quotes, \$vars and backticks" \
  '{"content": "cost is $5 \"today\" `now` \\ done\n"}' $CFS write $ROOT/a.md --rev "$REV"
body $ROOT/a.md | grep -q 'cost is \$5 "today" `now` \\ done' \
  && ok "shell metacharacters survive verbatim" || bad "shell metacharacters survive verbatim"

echo "=== stale rev rejected server-side ==="
expect_err_json "write with stale rev" "stale\|changed" '{"content":"clobber"}' \
  $CFS write $ROOT/a.md --rev "$REV"
body $ROOT/a.md | grep -q "clobber" && bad "stale write did not clobber" \
  || ok "stale write did not clobber"

echo "=== ambiguous edit ==="
REV=$(revof $ROOT/a.md)
expect_ok_json "write repeated content" '{"content": "todo\nkeep\ntodo\n"}' \
  $CFS write $ROOT/a.md --rev "$REV"
REV=$(revof $ROOT/a.md)
expect_err_json "ambiguous edit refused" "2 times" '{"old_str":"todo","new_str":"done"}' \
  $CFS edit $ROOT/a.md --rev "$REV"
expect_ok_json "disambiguated by context" '{"old_str":"todo\nkeep","new_str":"done\nkeep"}' \
  $CFS edit $ROOT/a.md --rev "$REV"

echo "=== edit --all ==="
REV=$(revof $ROOT/a.md)
expect_ok_json "seed repeated term" '{"content": "cat\ndog\ncat\ncat\n"}' \
  $CFS write $ROOT/a.md --rev "$REV"
REV=$(revof $ROOT/a.md)
expect_err_json "still ambiguous without --all" "2 times\|3 times" \
  '{"old_str":"cat","new_str":"lion"}' $CFS edit $ROOT/a.md --rev "$REV"
expect_ok_json "edit --all replaces every occurrence" \
  '{"old_str":"cat","new_str":"lion"}' $CFS edit $ROOT/a.md --rev "$REV" --all
[ "$(body $ROOT/a.md | grep -c lion)" = "3" ] && ok "all three replaced" \
  || bad "all three replaced"
body $ROOT/a.md | grep -q "dog" && ok "non-matching lines untouched" \
  || bad "non-matching lines untouched"
REV=$(revof $ROOT/a.md)
expect_err_json "--all still requires a match" "verbatim" \
  '{"old_str":"zebra","new_str":"x"}' $CFS edit $ROOT/a.md --rev "$REV" --all

echo "=== grep ==="
expect_ok_json "seed grep corpus" '{"content": "alpha BETA\ngamma\n"}' \
  $CFS write $ROOT/g1.md --new
expect_ok_json "seed second file" '{"content": "delta\nbeta two\n"}' \
  $CFS write $ROOT/g2.md --new
$CFS grep "beta" --path $ROOT | grep -q "g2.md" && ok "grep finds a match" \
  || bad "grep finds a match"
$CFS grep "beta" --path $ROOT | grep -q "g1.md" && bad "grep is case-sensitive by default" \
  || ok "grep is case-sensitive by default"
$CFS grep "beta" --path $ROOT -i | grep -q "g1.md" && ok "grep -i matches case-insensitively" \
  || bad "grep -i matches case-insensitively"
$CFS grep "al.ha|delta" --path $ROOT | grep -q "g1.md" && ok "grep supports regex alternation" \
  || bad "grep supports regex alternation"
$CFS grep "gamma" --path $ROOT -C 1 | grep -q "alpha BETA" && ok "grep -C shows context" \
  || bad "grep -C shows context"
# -l must list paths without any matched content lines.
OUT=$($CFS grep "beta" --path $ROOT -i -l)
if echo "$OUT" | grep -q "g1.md" && ! echo "$OUT" | grep -q "alpha"; then
  ok "grep -l prints paths without content"
else bad "grep -l prints paths without content"; fi
$CFS grep "nothingmatchesthis" --path $ROOT | grep -qi "no matches" \
  && ok "grep reports no matches cleanly" || bad "grep reports no matches cleanly"
expect_err "invalid regex rejected" "Invalid regular expression" \
  $CFS grep "unclosed[" --path $ROOT
# grep must see a file written moments ago -- the async-index failure it exists to avoid
expect_ok_json "write a file then immediately grep it" '{"content": "freshlywritten\n"}' \
  $CFS write $ROOT/g3.md --new
$CFS grep "freshlywritten" --path $ROOT | grep -q "g3.md" \
  && ok "grep finds a just-written file" || bad "grep finds a just-written file"
python -c "open('bin0.tmp','wb').write(bytes(range(256)))"
$CFS upload $ROOT/blob0.bin --from bin0.tmp --new >/dev/null 2>&1
$CFS grep "." --path $ROOT >/dev/null 2>&1 && ok "grep skips binaries without erroring" \
  || bad "grep skips binaries without erroring"

echo "=== diff ==="
# A real diff requires a change small in both absolute and relative terms:
# 100 lines with one edited is 2 changed lines at 2%, comfortably eyeballable.
python -c "print('\n'.join('line %d'%i for i in range(100)))" > d1.tmp
$CFS upload $ROOT/d.md --from d1.tmp --new >/dev/null
D1=$(revof $ROOT/d.md)
python -c "
ls=['line %d'%i for i in range(100)]; ls[50]='EDITED LINE'
print('\n'.join(ls))" > d2.tmp
$CFS upload $ROOT/d.md --from d2.tmp --rev "$D1" >/dev/null
OUT=$($CFS diff $ROOT/d.md)
echo "$OUT" | grep -q -- "-line 50" && ok "diff shows the removed line" \
  || bad "diff shows the removed line"
echo "$OUT" | grep -q -- "+EDITED LINE" && ok "diff shows the added line" \
  || bad "diff shows the added line"
echo "$OUT" | grep -q "2 changed line" && ok "diff reports the change count" \
  || bad "diff reports the change count"
echo "$OUT" | grep -q "rev: " && bad "diff withholds the current rev" \
  || ok "diff withholds the current rev"
rm -f d1.tmp d2.tmp
D1B=$(revof $ROOT/d.md)
$CFS diff $ROOT/d.md --rev "$D1B" --to "$D1B" | grep -qi "no difference" \
  && ok "diff of a rev against itself reports no difference" \
  || bad "diff of a rev against itself reports no difference"

# A small file is below the 5% threshold for any change at all -- by design it
# returns the file rather than a diff, since reading it whole is just as easy.
expect_ok_json "seed a small file" '{"content":"alpha\nbeta\ngamma\n"}' \
  $CFS write $ROOT/small.md --new
SREV=$(revof $ROOT/small.md)
expect_ok_json "change one line of it" '{"content":"alpha\nBETA\ngamma\n"}' \
  $CFS write $ROOT/small.md --rev "$SREV"
OUT=$($CFS diff $ROOT/small.md)
echo "$OUT" | grep -q "BETA" && ok "small file returns whole content, not a diff" \
  || bad "small file returns whole content, not a diff"
echo "$OUT" | grep -qi "too much to read as a diff" \
  && ok "small file explains why it is not a diff" \
  || bad "small file explains why it is not a diff"
# A file rewritten wholesale should refuse rather than emit pages of noise.
D2=$(revof $ROOT/d.md)
python -c "print('\n'.join('old line %d'%i for i in range(120)))" > big.tmp
$CFS upload $ROOT/d.md --from big.tmp --rev "$D2" >/dev/null
D3=$(revof $ROOT/d.md)
python -c "print('\n'.join('new line %d'%i for i in range(120)))" > big2.tmp
$CFS upload $ROOT/d.md --from big2.tmp --rev "$D3" >/dev/null
OUT=$($CFS diff $ROOT/d.md)
echo "$OUT" | grep -qi "too much to read as a diff" \
  && ok "wholesale rewrite declines the diff" || bad "wholesale rewrite declines the diff"
echo "$OUT" | grep -q "new line 5" \
  && ok "declined diff returns the current file instead" \
  || bad "declined diff returns the current file instead"
echo "$OUT" | grep -q "rev: " && bad "declined diff withholds the current rev" \
  || ok "declined diff withholds the current rev"
$CFS diff $ROOT/d.md --force | grep -q -- "+new line 5" \
  && ok "--force overrides the cap" || bad "--force overrides the cap"
rm -f big.tmp big2.tmp

echo "=== read --rev ==="
$CFS read $ROOT/d.md --rev "$D3" | grep -q "old line 5" \
  && ok "read --rev returns the historical content" \
  || bad "read --rev returns the historical content"
CURD=$(revof $ROOT/d.md)
$CFS read $ROOT/d.md --rev "$D3" | grep -q "$CURD" \
  && bad "read --rev withholds the current rev" || ok "read --rev withholds the current rev"
$CFS read $ROOT/d.md --rev "$D3" | grep -qi "cannot be used to write" \
  && ok "read --rev says it cannot license a write" \
  || bad "read --rev says it cannot license a write"

echo "=== head no longer harvests a rev (the case-status.md incident) ==="
# The failure this defends against: piping read through head to grab a rev
# while discarding the content the rev is supposed to certify. Content now
# comes before the rev, so head -3 on a multi-line file sees no rev at all.
expect_ok_json "seed a multi-line file" '{"content":"one\ntwo\nthree\nfour\nfive\n"}' \
  $CFS write $ROOT/head.md --new
HEADOUT=$($CFS read $ROOT/head.md | head -3)
echo "$HEADOUT" | grep -q "rev:" && bad "head -3 yields no rev" || ok "head -3 yields no rev"
FULLOUT=$($CFS read $ROOT/head.md)
echo "$FULLOUT" | grep -q "^rev: " && ok "the full read still discloses a rev" \
  || bad "the full read still discloses a rev"
echo "$FULLOUT" | grep -q "(5 line(s))" && ok "header states the total line count" \
  || bad "header states the total line count"
echo "$FULLOUT" | grep -qi "only valid if you read all" \
  && ok "rev line warns it is void without a full read" \
  || bad "rev line warns it is void without a full read"
# Dropbox keeps revision history per path across delete-and-recreate, so a fixed
# name would inherit revisions from previous runs and stop being single-revision.
ONCE="$ROOT/once-$$-$(date +%s).md"
expect_ok_json "single-revision file" '{"content":"only\n"}' $CFS write "$ONCE" --new
$CFS diff "$ONCE" | grep -qi "only one revision" \
  && ok "single-revision file explains itself" || bad "single-revision file explains itself"

echo "=== old_str mismatch diagnostics ==="
# old_str must genuinely fail to match: a substring of a line still matches, so
# these seeds differ from the file only in whitespace that spans a line break.
expect_ok_json "seed trailing-space file" '{"content":"foo   \nbar\n"}' \
  $CFS write $ROOT/ws1.md --new
echo '{"old_str":"foo\nbar","new_str":"z"}' \
  | $CFS edit $ROOT/ws1.md --rev "$(revof $ROOT/ws1.md)" 2>&1 \
  | grep -qi "trailing whitespace" \
  && ok "trailing-whitespace mismatch explained" || bad "trailing-whitespace mismatch explained"

expect_ok_json "seed indented file" '{"content":"    hello\n    world\n"}' \
  $CFS write $ROOT/ws2.md --new
echo '{"old_str":"hello\nworld","new_str":"z"}' \
  | $CFS edit $ROOT/ws2.md --rev "$(revof $ROOT/ws2.md)" 2>&1 \
  | grep -qi "indentation" \
  && ok "indentation mismatch explained" || bad "indentation mismatch explained"

expect_ok_json "seed typo file" '{"content":"the quick brown fox jumps\nnext line\n"}' \
  $CFS write $ROOT/ws3.md --new
echo '{"old_str":"the quick brwon fox jumps","new_str":"z"}' \
  | $CFS edit $ROOT/ws3.md --rev "$(revof $ROOT/ws3.md)" 2>&1 \
  | grep -qi "closest line" \
  && ok "near-miss suggests the closest line" || bad "near-miss suggests the closest line"

echo "=== history and restore (corrupt, then recover) ==="
GOOD=$(revof $ROOT/a.md)
GOODBODY=$(body $ROOT/a.md)
expect_ok_json "corrupt the file" '{"content": "CORRUPTED BY A BAD WRITE\n"}' \
  $CFS write $ROOT/a.md --rev "$GOOD"
body $ROOT/a.md | grep -q "CORRUPTED" && ok "corruption landed" || bad "corruption landed"
$CFS history $ROOT/a.md | grep -q "$GOOD" && ok "history lists the pre-corruption rev" \
  || bad "history lists the pre-corruption rev"
expect_ok "restore to the good rev" $CFS restore $ROOT/a.md --rev "$GOOD"
if [ "$(body $ROOT/a.md)" = "$GOODBODY" ]; then ok "restore recovered exact content"
else bad "restore recovered exact content"; fi
body $ROOT/a.md | grep -q "CORRUPTED" && bad "corruption gone" || ok "corruption gone"

echo "=== rev disclosure audit: every command, against the real rev value ==="
# A rev is evidence that a file has been read. Any command that hands one out
# without a read mints that evidence for free and voids read-before-write.
#
# These assert on the ACTUAL current rev string, never on formatting like
# "rev: ". An earlier version of this audit grepped for the prefix, so `diff`
# and `history` printed the live rev bare and passed anyway.
CUR=$(revof $ROOT/g1.md)
[ -n "$CUR" ] && ok "audit has a real rev to test against ($CUR)" \
  || bad "audit has a real rev to test against"

# withholds <label> <command...>  -- fails if the current rev appears in output
withholds() {
  local d="$1"; shift
  if "$@" 2>&1 | grep -q "$CUR"; then bad "$d withholds the current rev"
  else ok "$d withholds the current rev"; fi
}

withholds "list"        $CFS list $ROOT --depth 5
withholds "search"      $CFS search "freshlywritten" --path $ROOT
withholds "grep"        $CFS grep "alpha" --path $ROOT
withholds "history"     $CFS history $ROOT/g1.md
withholds "diff"        $CFS diff $ROOT/g1.md
withholds "read --rev"  $CFS read $ROOT/g1.md --rev "$(prev_rev $ROOT/g1.md)"

STALE_OUT=$(echo '{"old_str":"alpha","new_str":"x"}' \
  | $CFS edit $ROOT/g1.md --rev 0123456789 2>&1)
echo "$STALE_OUT" | grep -q "$CUR" \
  && bad "stale edit error withholds the current rev" \
  || ok "stale edit error withholds the current rev"
echo "$STALE_OUT" | grep -qi "re-read" \
  && ok "stale edit error says to re-read" || bad "stale edit error says to re-read"
STALE_W=$(echo '{"content":"x"}' | $CFS write $ROOT/g1.md --rev 0123456789 2>&1)
echo "$STALE_W" | grep -q "$CUR" \
  && bad "stale write error withholds the current rev" \
  || ok "stale write error withholds the current rev"

# The positive case: read must still hand out a usable rev, or the whole
# scheme is unusable rather than merely safe.
$CFS read $ROOT/g1.md | grep -q "$CUR" && ok "read discloses the current rev" \
  || bad "read discloses the current rev"

# history must still expose OLDER revs -- restore depends on them.
$CFS history $ROOT/g1.md | grep -q "rev:" \
  && ok "history still exposes older revs for restore" \
  || bad "history still exposes older revs for restore"

echo "=== --new conflict is diagnosed correctly (not as a stale rev) ==="
OUT=$(echo '{"content":"x"}' | $CFS write $ROOT/g1.md --new 2>&1)
echo "$OUT" | grep -qi "already exists" && ok "--new conflict says 'already exists'" \
  || bad "--new conflict says 'already exists' (got: $OUT)"
echo "$OUT" | grep -qi "stale\|changed since you read" \
  && bad "--new conflict does not misdiagnose as stale" \
  || ok "--new conflict does not misdiagnose as stale"
body $ROOT/g1.md | grep -q "alpha" && ok "--new conflict wrote nothing" \
  || bad "--new conflict wrote nothing"
python -c "open('bin1.tmp','wb').write(b'zz')"
$CFS upload $ROOT/g1.md --from bin1.tmp --new 2>&1 | grep -qi "already exists" \
  && ok "upload --new conflict diagnosed the same way" \
  || bad "upload --new conflict diagnosed the same way"
rm -f bin1.tmp

echo "=== restore withholds the new rev ==="
R_OUT=$($CFS restore $ROOT/g1.md --rev "$(revof $ROOT/g1.md)" 2>&1)
echo "$R_OUT" | grep -q "new rev:" && bad "restore withholds the new rev" \
  || ok "restore withholds the new rev"
echo "$R_OUT" | grep -qi "read" && ok "restore tells you to read" || bad "restore tells you to read"

echo "=== protected roots ==="
expect_err "delete /memory refused" "Refusing to delete" $CFS delete /memory --force
expect_err "rename /memory refused" "Refusing to rename" $CFS rename /memory /memory-old
expect_err "delete / refused" "Refusing to delete" $CFS delete / --force
$CFS list / --depth 1 | grep -q "/memory" && ok "/memory still intact" \
  || bad "/memory still intact"
expect_ok_json "entries inside /memory are still deletable" '{"content":"tmp\n"}' \
  $CFS write /memory/_probe.md --new
expect_ok "delete an entry inside /memory" \
  $CFS delete /memory/_probe.md --rev "$(revof /memory/_probe.md)"

echo "=== nested paths and rename ==="
expect_ok_json "write into a nested path" '{"content":"nested\n"}' \
  $CFS write $ROOT/sub/b.md --new
$CFS list $ROOT --depth 5 | grep -q "b.md" && ok "parent dir created implicitly" \
  || bad "parent dir created implicitly"
expect_ok "rename" $CFS rename $ROOT/sub/b.md $ROOT/sub/c.md
$CFS list $ROOT --depth 5 | grep -q "c.md" && ok "rename took effect" \
  || bad "rename took effect"
$CFS list $ROOT --depth 5 | grep -q "b.md" && bad "old name gone" || ok "old name gone"

echo "=== copy ==="
expect_ok "copy" $CFS copy $ROOT/a.md $ROOT/a-copy.md
$CFS list $ROOT --depth 5 | grep -q "a-copy.md" && ok "copy took effect" || bad "copy took effect"
expect_err "copy onto existing path" "conflict" $CFS copy $ROOT/a.md $ROOT/a-copy.md

echo "=== upload / download (binary) ==="
python -c "open('bin.tmp','wb').write(bytes(range(256))*8)"
expect_ok "upload binary" $CFS upload $ROOT/blob.bin --from bin.tmp --new
expect_err "upload overwrite without rev" "without --rev" \
  $CFS upload $ROOT/blob.bin --from bin.tmp
expect_ok "download binary" $CFS download $ROOT/blob.bin --to out.tmp
if cmp -s bin.tmp out.tmp; then ok "binary round-trips byte-identical"
else bad "binary round-trips byte-identical"; fi
expect_err "upload from missing local file" "Could not read local file" \
  $CFS upload $ROOT/x.bin --from ./nope.tmp --new

echo "=== search ==="
$CFS search "CORRUPTED" --path $ROOT >/dev/null 2>&1 && ok "search runs" || bad "search runs"
$CFS search "a-copy" --path $ROOT --names-only 2>&1 | grep -qi "match\|No matches" \
  && ok "filename search runs" || bad "filename search runs"

echo "=== traversal ==="
expect_err "path traversal refused" "\.\." $CFS read "$ROOT/../../etc/passwd"

echo "=== delete ==="
expect_ok "delete nested file" $CFS delete $ROOT/sub/c.md --rev "$(revof $ROOT/sub/c.md)"
expect_err "delete without rev" "without --rev" $CFS delete $ROOT/a-copy.md
REV=$(revof $ROOT/a-copy.md)
expect_ok "delete with rev" $CFS delete $ROOT/a-copy.md --rev "$REV"
expect_err "read after delete" "not_found\|does not exist" $CFS read $ROOT/a-copy.md

echo "=== cleanup ==="
expect_ok "delete tree with --force" $CFS delete $ROOT --force
rm -f bin.tmp out.tmp bin0.tmp

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
