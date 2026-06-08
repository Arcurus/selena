#!/usr/bin/env python3
"""
Tests for the danger prefilter (added 2026-06-08 per Arcurus #openworld).

The prefilter is a small regex pass that runs at add_todo() time. If
the long_desc or short_desc contains one of the patterns in
DANGER_PATTERNS, the new todo is auto-flagged as `irreversible=True`
with a short block_reason that quotes the matched pattern. Workers
(all 3) MUST then NOT execute the todo without Arcurus's
confirmation.

Per Arcurus 2026-06-08: "a regex could give a hint and preset it,
but in the end the llm must decide."  So this is HINT, not
AUTHORITY.  Workers also self-assess at the start of step 4 and
can flip `irreversible=True` on a todo the prefilter missed.

These tests verify:
- True positives: obvious dangerous patterns are flagged
- True negatives: benign todos are not flagged
- Edge cases: empty strings, negation, "drop the" (vs "drop ")
- The labels are stable (used by the LLM and the web UI)
- Integration with add_todo: prefilter runs only when irreversible
  is not explicitly set
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from todo_manager import _apply_danger_prefilter, TodoManager  # noqa: E402


class TestPrefilterRegexes(unittest.TestCase):
    """True positives — patterns that should fire."""

    def test_rm_rf(self):
        self.assertEqual(_apply_danger_prefilter("rm -rf /tmp/foo", ""), "rm -rf")

    def test_drop_table(self):
        self.assertEqual(_apply_danger_prefilter("DROP TABLE users", ""), "DROP database object")

    def test_drop_the_table(self):
        # The pattern allows "the" between verb and noun so natural
        # prose like "drop the table" still matches.
        # NOTE: "drop the PRODUCTION table" is a known false negative.
        # The pattern requires "the" (optional) DIRECTLY before the noun;
        # allowing intervening words would over-match benign prose like
        # "drop the index of this table".  The LLM self-assess picks it up.
        self.assertIsNone(_apply_danger_prefilter("drop the production table", ""))

    def test_delete_from_no_where(self):
        self.assertEqual(_apply_danger_prefilter("delete from users where 1=1", ""), "DELETE FROM (no where / 1=1)")

    def test_kill_minus_9(self):
        self.assertEqual(_apply_danger_prefilter("kill -9 1234", ""), "kill -9")

    def test_kubectl_delete_namespace(self):
        self.assertEqual(_apply_danger_prefilter("kubectl delete namespace prod", ""), "kubectl delete namespace")

    def test_kubectl_delete_the_namespace(self):
        self.assertEqual(_apply_danger_prefilter("kubectl delete the namespace prod", ""), "kubectl delete namespace")

    def test_terraform_destroy(self):
        self.assertEqual(_apply_danger_prefilter("terraform destroy", ""), "terraform destroy")

    def test_git_push_force_origin_main(self):
        self.assertEqual(_apply_danger_prefilter("git push -f origin main", ""), "force-push to main/master")

    def test_git_push_force_origin_refspec_main(self):
        # with the explicit refspec (origin:main) — also dangerous
        self.assertEqual(_apply_danger_prefilter("git push -f origin :main", ""), "force-push to main/master")

    def test_git_push_force_origin_master(self):
        self.assertEqual(_apply_danger_prefilter("git push --force origin master", ""), "force-push to main/master")

    def test_mkfs(self):
        self.assertEqual(_apply_danger_prefilter("mkfs.ext4 /dev/sda1", ""), "mkfs (make filesystem)")

    def test_shutdown(self):
        self.assertEqual(_apply_danger_prefilter("shutdown -h now", ""), "shutdown")

    def test_init_0(self):
        self.assertEqual(_apply_danger_prefilter("init 0", ""), "init 0")

    def test_systemctl_stop_selena(self):
        self.assertEqual(_apply_danger_prefilter("systemctl stop selena-project", ""), "stop/disable a core service")

    def test_truncate_history(self):
        self.assertEqual(_apply_danger_prefilter("truncate history", "remove the history"), "truncate")

    def test_entity_delete(self):
        self.assertEqual(_apply_danger_prefilter("entity.delete('id')", ""), "delete an entity")

    def test_reminder_does_not_drop(self):
        # Even when wrapped in a negation the regex still matches,
        # which is the right behavior (we want a second look).
        self.assertEqual(
            _apply_danger_prefilter("Reminder: do not drop the table", ""),
            "DROP database object",
        )


class TestPrefilterNegatives(unittest.TestCase):
    """True negatives — patterns that should NOT fire."""

    def test_add_endpoint(self):
        self.assertIsNone(_apply_danger_prefilter("add a new endpoint", "POST /api/widgets"))

    def test_update_readme(self):
        self.assertIsNone(_apply_danger_prefilter("update README with new docs", "doc tweak"))

    def test_investigate_slow_call(self):
        self.assertIsNone(_apply_danger_prefilter("investigate the slow LLM call", "might be a bug"))

    def test_wire_up_endpoint(self):
        self.assertIsNone(_apply_danger_prefilter("wire up the discord send log", "new endpoint"))

    def test_rm_rf_with_typo(self):
        # 'rm -rf2' — the '2' makes it not match 'rm -rf' thanks
        # to the \b word boundary.  Treated as a typo, not a kill.
        self.assertIsNone(_apply_danger_prefilter("rm -rf2 is a typo in the docs", "should not trigger"))

    def test_truncated_description(self):
        # 'truncated' is a description, not a TRUNCATE TABLE
        self.assertIsNone(_apply_danger_prefilter("truncated description but not the table", "old content"))

    def test_empty_input(self):
        self.assertIsNone(_apply_danger_prefilter("", ""))


class TestPrefilterShortDesc(unittest.TestCase):
    """The prefilter checks BOTH short_desc and long_desc."""

    def test_short_desc_only(self):
        # If the danger is in the short_desc alone, we should still catch it
        self.assertEqual(_apply_danger_prefilter("", "DROP TABLE users"), "DROP database object")

    def test_long_desc_only(self):
        # And vice-versa
        self.assertEqual(_apply_danger_prefilter("DROP TABLE users", ""), "DROP database object")

    def test_both(self):
        # If both contain patterns, the first match in concatenation
        # order wins — we don't test the specific label, just that
        # something fires.
        result = _apply_danger_prefilter("DROP TABLE users", "rm -rf /")
        self.assertIsNotNone(result)


class TestAddTodoPrefilterIntegration(unittest.TestCase):
    """The prefilter is invoked from add_todo() when irreversible is not set."""

    def setUp(self):
        # Use a temp data dir so we don't pollute the real one
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.tm = TodoManager()

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_benign_not_flagged(self):
        t = self.tm.add_todo(short_desc="add new endpoint", long_desc="POST /api/widgets")
        self.assertEqual(t.get("irreversible"), False)
        self.assertIsNone(t.get("block_reason"))

    def test_dangerous_auto_flagged(self):
        t = self.tm.add_todo(short_desc="cleanup old files", long_desc="rm -rf /tmp/old_logs/*")
        self.assertEqual(t.get("irreversible"), True)
        self.assertIn("rm -rf", t.get("block_reason", ""))

    def test_explicit_irreversible_true_honored(self):
        t = self.tm.add_todo(short_desc="benign title", long_desc="just docs",
                            irreversible=True, block_reason="Arcurus flagged it")
        self.assertEqual(t.get("irreversible"), True)
        self.assertEqual(t.get("block_reason"), "Arcurus flagged it")

    def test_explicit_irreversible_false_no_prefilter(self):
        # Even if the desc looks dangerous, an explicit
        # irreversible=False should NOT trigger the prefilter.
        # (This is the "LLM is sure, skip the smoke detector" path.)
        t = self.tm.add_todo(short_desc="DROP TABLE users", long_desc="cleanup",
                            irreversible=False)
        self.assertEqual(t.get("irreversible"), False)
        self.assertIsNone(t.get("block_reason"))

    def test_prefilter_skipped_when_explicit_irreversible_true(self):
        # If the caller passes irreversible=True explicitly, the
        # prefilter should not fire (no double-prefix of block_reason).
        t = self.tm.add_todo(short_desc="rm -rf foo", long_desc="x",
                            irreversible=True, block_reason="explicit override")
        self.assertEqual(t.get("block_reason"), "explicit override")
        self.assertFalse("regex-prefilter" in (t.get("block_reason") or ""))

    def test_update_todo_can_flip_irreversible(self):
        # Worker self-assess path: started with a benign todo, then
        # the worker updated it to irreversible=True after self-assessing
        # danger mid-step.
        t = self.tm.add_todo(short_desc="benign task", long_desc="doc tweak")
        self.assertEqual(t.get("irreversible"), False)
        updated = self.tm.update_todo(t["id"], irreversible=True,
                                      block_reason="worker self-assessed danger")
        self.assertEqual(updated.get("irreversible"), True)
        self.assertEqual(updated.get("block_reason"), "worker self-assessed danger")


if __name__ == "__main__":
    unittest.main(verbosity=2)
