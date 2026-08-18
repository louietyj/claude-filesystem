#!/usr/bin/env python3
"""Offline tests for the logic that does not need Dropbox."""

import io
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))

import cfs  # noqa: E402


class TestNormalise(unittest.TestCase):
    def test_adds_leading_slash(self):
        self.assertEqual(cfs.normalise("memory/INDEX.md"), "/memory/INDEX.md")

    def test_collapses_redundant_separators(self):
        self.assertEqual(cfs.normalise("//memory///a.md"), "/memory/a.md")
        self.assertEqual(cfs.normalise("/memory/./a.md"), "/memory/a.md")

    def test_rejects_traversal(self):
        for bad in ("/memory/../../secrets", "/memory/..", "../x"):
            with self.assertRaises(cfs.CfsError):
                cfs.normalise(bad)

    def test_rejects_url_encoded_traversal(self):
        with self.assertRaises(cfs.CfsError):
            cfs.normalise("/memory/%2e%2e/secrets")

    def test_root_maps_to_empty_api_path(self):
        self.assertEqual(cfs.api_path("/"), "")
        self.assertEqual(cfs.api_path("/memory"), "/memory")


class TestApplyReplacement(unittest.TestCase):
    def test_unique_match_replaced(self):
        self.assertEqual(
            cfs.apply_replacement("alpha beta gamma", "beta", "delta", "/f"),
            "alpha delta gamma",
        )

    def test_no_match_raises(self):
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.apply_replacement("alpha", "zeta", "x", "/f")
        self.assertIn("did not appear verbatim", str(ctx.exception))

    def test_ambiguous_match_raises_with_line_numbers(self):
        text = "todo\nkeep\ntodo\n"
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.apply_replacement(text, "todo", "done", "/f")
        message = str(ctx.exception)
        self.assertIn("appears 2 times", message)
        self.assertIn("lines 1, 3", message)

    def test_multiline_old_str(self):
        text = "a\nb\nc\n"
        self.assertEqual(cfs.apply_replacement(text, "a\nb", "z", "/f"), "z\nc\n")

    def test_ambiguity_error_mentions_the_all_escape(self):
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.apply_replacement("x\nx\n", "x", "y", "/f")
        self.assertIn("--all", str(ctx.exception))

    def test_replace_all_replaces_every_occurrence(self):
        self.assertEqual(
            cfs.apply_replacement("x\nkeep\nx\n", "x", "y", "/f", replace_all=True),
            "y\nkeep\ny\n",
        )

    def test_replace_all_still_requires_a_match(self):
        with self.assertRaises(cfs.CfsError):
            cfs.apply_replacement("abc", "zzz", "y", "/f", replace_all=True)


class TestRetryPolicy(unittest.TestCase):
    def _exc(self, code, headers=None):
        return urllib.error.HTTPError(
            "https://x", code, "err", headers or {}, None
        )

    def test_write_contention_is_retried(self):
        body = '{"error": {".tag": "too_many_write_operations"}}'
        self.assertIsNotNone(cfs._retry_after(self._exc(429), body, 0))

    def test_rate_limit_is_retried(self):
        self.assertIsNotNone(cfs._retry_after(self._exc(429), "rate_limit", 0))

    def test_503_is_retried(self):
        self.assertIsNotNone(cfs._retry_after(self._exc(503), "", 0))

    def test_conflict_is_not_retried(self):
        body = '{"error": {".tag": "path", "reason": {".tag": "conflict"}}}'
        self.assertIsNone(cfs._retry_after(self._exc(409), body, 0))

    def test_auth_failure_is_not_retried(self):
        self.assertIsNone(cfs._retry_after(self._exc(401), "missing_scope", 0))

    def test_gives_up_after_max_retries(self):
        self.assertIsNone(
            cfs._retry_after(self._exc(429), "too_many_write_operations", cfs.MAX_RETRIES)
        )

    def test_honours_retry_after_header(self):
        exc = self._exc(429, {"Retry-After": "3"})
        self.assertEqual(cfs._retry_after(exc, "too_many_", 0), 3.0)

    def test_honours_retry_after_in_body(self):
        body = '{"error": {".tag": "too_many_write_operations"}, "retry_after": 7}'
        self.assertEqual(cfs._retry_after(self._exc(429), body, 0), 7.0)

    def test_caps_absurd_retry_after(self):
        exc = self._exc(429, {"Retry-After": "9999"})
        self.assertEqual(cfs._retry_after(exc, "too_many_", 0), 30.0)

    def test_backoff_grows_and_is_jittered(self):
        delays = [
            cfs._retry_after(self._exc(429), "too_many_", n) for n in range(4)
        ]
        self.assertTrue(all(d is not None for d in delays))
        # Jitter must make identical attempts diverge, or parallel callers
        # resynchronise and collide again on the same instant.
        repeats = {cfs._retry_after(self._exc(429), "too_many_", 2) for _ in range(20)}
        self.assertGreater(len(repeats), 1)


class TestProtectedRoots(unittest.TestCase):
    def test_memory_root_cannot_be_deleted(self):
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.guard_protected("/memory", "delete")
        self.assertIn("Refusing to delete", str(ctx.exception))

    def test_trailing_slash_still_protected(self):
        with self.assertRaises(cfs.CfsError):
            cfs.guard_protected("/memory/", "delete")

    def test_entries_inside_memory_are_not_protected(self):
        cfs.guard_protected("/memory/hawaii.md", "delete")
        cfs.guard_protected("/memory/tack/INDEX.md", "delete")

    def test_similarly_named_paths_are_not_protected(self):
        cfs.guard_protected("/memory-old", "rename")
        cfs.guard_protected("/memories", "delete")


class TestFormatting(unittest.TestCase):
    def test_numbered_is_one_indexed_and_tab_separated(self):
        self.assertEqual(cfs.numbered("x\ny"), "     1\tx\n     2\ty")

    def test_numbered_respects_start_offset(self):
        self.assertEqual(cfs.numbered("x", start=10), "    10\tx")

    def test_human_size(self):
        self.assertEqual(cfs.human_size(512), "512B")
        self.assertEqual(cfs.human_size(2048), "2.0K")
        self.assertEqual(cfs.human_size(5 * 1024 * 1024), "5.0M")


class TestReadPayload(unittest.TestCase):
    """stdin is replaced with a StringIO; isatty() is False on those, as in a pipe."""

    def _stdin(self, text):
        sys.stdin = io.StringIO(text)
        self.addCleanup(setattr, sys, "stdin", sys.__stdin__)

    def test_parses_object(self):
        self._stdin('{"old_str": "a", "new_str": "b"}')
        payload = cfs.read_payload(("old_str", "new_str"), "example")
        self.assertEqual(payload["old_str"], "a")

    def test_escaped_newlines_become_real_newlines(self):
        self._stdin('{"content": "one\\ntwo\\n"}')
        self.assertEqual(cfs.read_payload(("content",), "ex")["content"], "one\ntwo\n")

    def test_embedded_quotes_and_backslashes_survive(self):
        self._stdin(r'{"content": "say \"hi\" and \\ done"}')
        self.assertEqual(
            cfs.read_payload(("content",), "ex")["content"], 'say "hi" and \\ done'
        )

    def test_malformed_json_raises_clean_error(self):
        self._stdin('{"old_str": "unterminated}')
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_payload(("old_str",), "example")
        self.assertIn("Could not parse stdin as JSON", str(ctx.exception))

    def test_missing_key_raises(self):
        self._stdin('{"old_str": "a"}')
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_payload(("old_str", "new_str"), "example")
        self.assertIn("new_str", str(ctx.exception))

    def test_non_string_value_raises(self):
        self._stdin('{"content": 42}')
        with self.assertRaises(cfs.CfsError):
            cfs.read_payload(("content",), "example")

    def test_empty_stdin_raises(self):
        self._stdin("   ")
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_payload(("content",), "example")
        self.assertIn("Empty stdin", str(ctx.exception))

    def test_array_rejected(self):
        self._stdin("[1, 2]")
        with self.assertRaises(cfs.CfsError):
            cfs.read_payload(("content",), "example")


class TestSearchReplace(unittest.TestCase):
    def parse(self, text, tag=None):
        return cfs.parse_search_replace(text, tag)

    def test_single_block(self):
        raw = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        self.assertEqual(self.parse(raw), [("old", "new")])

    def test_multiline_sides(self):
        raw = "<<<<<<< SEARCH\na\nb\n=======\nA\nB\n>>>>>>> REPLACE\n"
        self.assertEqual(self.parse(raw), [("a\nb", "A\nB")])

    def test_parser_reports_every_block_so_edit_can_refuse_a_batch(self):
        # The parser sees them all; edit rejects more than one, because block
        # syntax is the most error-prone part and batching compounds it.
        raw = (
            "<<<<<<< SEARCH\na\n=======\nA\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nb\n=======\nB\n>>>>>>> REPLACE\n"
        )
        self.assertEqual(self.parse(raw), [("a", "A"), ("b", "B")])

    def test_empty_replace_is_a_deletion(self):
        raw = "<<<<<<< SEARCH\ngone\n=======\n>>>>>>> REPLACE\n"
        self.assertEqual(self.parse(raw), [("gone", "")])

    def test_content_with_shell_and_json_hostile_chars(self):
        raw = '<<<<<<< SEARCH\nsay "hi" $HOME `x` \\ done\n=======\nnew\n>>>>>>> REPLACE\n'
        self.assertEqual(self.parse(raw)[0][0], 'say "hi" $HOME `x` \\ done')

    def test_setext_heading_underline_is_not_a_divider(self):
        # Markdown H1 underlines are '=' runs; only exactly seven counts.
        raw = "<<<<<<< SEARCH\nTitle\n=====\n=========\n=======\nnew\n>>>>>>> REPLACE\n"
        old, new = self.parse(raw)[0]
        self.assertEqual(old, "Title\n=====\n=========")
        self.assertEqual(new, "new")

    def test_repeated_divider_as_a_closer_is_rejected(self):
        # The exact mistake seen in the wild: closing the block by repeating
        # the separator instead of using the REPLACE marker.
        raw = "<<<<<<< SEARCH\nold\n=======\nnew\n=======\n>>>>>>> REPLACE\n"
        with self.assertRaises(cfs.CfsError) as ctx:
            self.parse(raw)
        message = str(ctx.exception)
        self.assertIn("second", message)
        self.assertIn("do not repeat the divider", message)

    def test_missing_divider_is_rejected(self):
        raw = "<<<<<<< SEARCH\nold\n>>>>>>> REPLACE\n"
        with self.assertRaises(cfs.CfsError) as ctx:
            self.parse(raw)
        self.assertIn("without a", str(ctx.exception))

    def test_unclosed_block_is_rejected(self):
        raw = "<<<<<<< SEARCH\nold\n=======\nnew\n"
        with self.assertRaises(cfs.CfsError) as ctx:
            self.parse(raw)
        self.assertIn("never closed", str(ctx.exception))

    def test_nested_start_is_rejected(self):
        raw = "<<<<<<< SEARCH\na\n=======\nA\n<<<<<<< SEARCH\n"
        with self.assertRaises(cfs.CfsError) as ctx:
            self.parse(raw)
        self.assertIn("never closed", str(ctx.exception))

    def test_no_block_at_all_is_rejected(self):
        with self.assertRaises(cfs.CfsError) as ctx:
            self.parse("just some text\n")
        self.assertIn("No SEARCH/REPLACE block", str(ctx.exception))


class TestSearchReplaceTag(unittest.TestCase):
    TAG = "@@X@@"

    def test_tagged_markers_parse(self):
        raw = (
            f"<<<<<<< SEARCH {self.TAG}\nold\n======= {self.TAG}\nnew\n"
            f">>>>>>> REPLACE {self.TAG}\n"
        )
        self.assertEqual(cfs.parse_search_replace(raw, self.TAG), [("old", "new")])

    def test_untagged_markers_inside_content_are_literal(self):
        # The whole point: a file containing real conflict markers can still be
        # edited, because only the tagged lines are structural.
        raw = (
            f"<<<<<<< SEARCH {self.TAG}\n"
            "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch\n"
            f"======= {self.TAG}\nresolved\n>>>>>>> REPLACE {self.TAG}\n"
        )
        old, new = cfs.parse_search_replace(raw, self.TAG)[0]
        self.assertEqual(old, "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch")
        self.assertEqual(new, "resolved")

    def test_tagged_mode_ignores_untagged_blocks_entirely(self):
        raw = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        with self.assertRaises(cfs.CfsError):
            cfs.parse_search_replace(raw, self.TAG)


class TestMarkerDetection(unittest.TestCase):
    def test_detects_conflict_markers_in_a_file(self):
        self.assertTrue(cfs.file_has_marker_lines("a\n<<<<<<< HEAD\nb\n"))
        self.assertTrue(cfs.file_has_marker_lines("a\n=======\nb\n"))
        self.assertTrue(cfs.file_has_marker_lines("a\n>>>>>>> branch\n"))

    def test_ordinary_markdown_is_not_flagged(self):
        self.assertFalse(cfs.file_has_marker_lines("# Title\n\n- a\n- b\n"))

    def test_setext_headings_are_not_flagged(self):
        # Only exactly seven '=' is structural, so real headings pass.
        self.assertFalse(cfs.file_has_marker_lines("Title\n=====\n\nBody\n"))
        self.assertFalse(cfs.file_has_marker_lines("Title\n==========\n"))

    def test_exactly_seven_equals_is_flagged(self):
        self.assertTrue(cfs.file_has_marker_lines("Title\n=======\n"))


class TestReadDelimited(unittest.TestCase):
    def _stdin(self, text):
        sys.stdin = io.StringIO(text)
        self.addCleanup(setattr, sys, "stdin", sys.__stdin__)

    def test_splits_on_the_marker_line(self):
        self._stdin("old line\n@@\nnew line\n")
        self.assertEqual(cfs.read_delimited("@@"), ("old line", "new line"))

    def test_multiline_both_sides(self):
        self._stdin("a\nb\n@@\nc\nd\n")
        self.assertEqual(cfs.read_delimited("@@"), ("a\nb", "c\nd"))

    def test_empty_new_side_is_a_deletion(self):
        self._stdin("gone\n@@\n")
        self.assertEqual(cfs.read_delimited("@@"), ("gone", ""))

    def test_content_needing_json_escapes_passes_through_untouched(self):
        raw = 'say "hi" $HOME `now` \\ done\n@@\nreplaced\n'
        self._stdin(raw)
        old, new = cfs.read_delimited("@@")
        self.assertEqual(old, 'say "hi" $HOME `now` \\ done')
        self.assertEqual(new, "replaced")

    def test_marker_must_be_a_whole_line(self):
        # "@@" inside a line is content, not a delimiter.
        self._stdin("prefix @@ suffix\n@@\nnew\n")
        self.assertEqual(cfs.read_delimited("@@"), ("prefix @@ suffix", "new"))

    def test_missing_delimiter_raises(self):
        self._stdin("no marker here\n")
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_delimited("@@")
        self.assertIn("not found", str(ctx.exception))

    def test_duplicate_delimiter_raises_with_line_numbers(self):
        self._stdin("a\n@@\nb\n@@\nc\n")
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_delimited("@@")
        message = str(ctx.exception)
        self.assertIn("appears 2 times", message)
        self.assertIn("lines 2, 4", message)

    def test_empty_delimiter_is_rejected(self):
        # A blank marker would match the empty final element every heredoc
        # produces, so it can never be unique. Reject it by name rather than
        # letting it fail as a confusing duplicate.
        self._stdin("old\n\nnew\n")
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.read_delimited("")
        self.assertIn("must not be empty", str(ctx.exception))


class TestNoFileIndirection(unittest.TestCase):
    """old_str must be generated inline; reading it from a file would defeat
    the proof-of-knowledge that matching on old_str exists to provide."""

    def test_edit_parser_has_no_old_or_new_flags(self):
        parser = cfs.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["edit", "/f", "--rev", "r", "--old", "a", "--new", "b"])

    def test_write_content_flag_does_not_read_files(self):
        args = cfs.build_parser().parse_args(
            ["write", "/f", "--new", "--content", "@/etc/passwd"]
        )
        self.assertEqual(args.content, "@/etc/passwd")  # literal, not a file read


class TestWriteMode(unittest.TestCase):
    def _args(self, argv):
        return cfs.build_parser().parse_args(argv)

    def test_new_uses_add(self):
        self.assertEqual(cfs.write_mode(self._args(["write", "/f", "--new"]), "/f"), "add")

    def test_rev_uses_update_cas(self):
        mode = cfs.write_mode(self._args(["write", "/f", "--rev", "abc"]), "/f")
        self.assertEqual(mode, {".tag": "update", "update": "abc"})

    def test_neither_raises(self):
        with self.assertRaises(cfs.CfsError) as ctx:
            cfs.write_mode(self._args(["write", "/f"]), "/f")
        self.assertIn("without --rev", str(ctx.exception))

    def test_both_raises(self):
        with self.assertRaises(cfs.CfsError):
            cfs.write_mode(self._args(["write", "/f", "--new", "--rev", "abc"]), "/f")

    def test_upload_shares_the_same_rule(self):
        args = self._args(["upload", "/f", "--from", "x.png"])
        with self.assertRaises(cfs.CfsError):
            cfs.write_mode(args, "/f")


if __name__ == "__main__":
    unittest.main(verbosity=2)
