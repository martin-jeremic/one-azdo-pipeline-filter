import tempfile
import threading
import time
import unittest
from pathlib import Path

from ado import AdoError
from store import RunStore
from tests.fakes import FakeClient
from tests.samples import APPROVAL, PR_SOURCE_SHA, pr_build, waiting_build


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name)

    def store(self, client):
        return RunStore(client, self.cache_dir, clock=lambda: self.now[0])

    def test_runs_are_cached_until_max_age(self):
        client = FakeClient()
        store = self.store(client)
        store.runs("o", "p", "30d", max_age=300)
        self.now[0] += 299
        store.runs("O", "P", "30d", max_age=300)
        self.assertEqual(client.build_calls, 1)
        self.now[0] += 1
        store.runs("o", "p", "30d", max_age=300)
        self.assertEqual(client.build_calls, 2)

    def test_each_window_is_loaded_separately_and_invalidate_forces_a_reload(self):
        client = FakeClient()
        store = self.store(client)
        store.runs("o", "p", "30d", max_age=300)
        store.runs("o", "p", "7d", max_age=300)
        store.invalidate("o", "p", "30d")
        store.runs("o", "p", "30d", max_age=300)
        self.assertEqual(client.build_calls, 3)

    def test_pr_builds_get_their_pushed_commit_and_it_is_kept_on_disk(self):
        client = FakeClient()
        pr = next(r for r in self.store(client).runs("o", "p", "30d", 300).runs if r.pr_number)
        self.assertEqual(pr.pr_source_sha, PR_SOURCE_SHA)
        self.assertEqual(pr.pr_source_branch, "feature/order-export")
        self.assertTrue((self.cache_dir / "merge-commits.json").exists())

        second = FakeClient()
        pr = next(r for r in self.store(second).runs("o", "p", "30d", 300).runs if r.pr_number)
        self.assertEqual(pr.pr_source_sha, PR_SOURCE_SHA)
        self.assertEqual(second.commit_calls, 0)

    def test_failed_lookups_are_reported_and_retried_next_load(self):
        client = FakeClient(builds=[pr_build(), pr_build(id=12350)], commits={})
        store = self.store(client)
        snapshot = store.runs("o", "p", "30d", 300)
        self.assertEqual(snapshot.unresolved, 2)
        self.assertIn("not found", snapshot.unresolved_reason)
        self.assertEqual(client.commit_calls, 1)  # both builds share one merge commit
        store.invalidate("o", "p", "30d")
        store.runs("o", "p", "30d", 300)
        self.assertEqual(client.commit_calls, 2)

    def waiting_run(self, snapshot):
        return next(r for r in snapshot.runs if r.id == 12363)

    def test_runs_held_at_an_approval_are_waiting_with_stage_and_approvers(self):
        client = FakeClient(builds=[waiting_build(), pr_build()], approvals=[APPROVAL])
        snapshot = self.store(client).runs("o", "p", "30d", 300)
        run = self.waiting_run(snapshot)
        self.assertEqual(run.state, "waitingForApproval")
        self.assertEqual([a.stage for a in run.approvals], ["Deploy to Staging"])
        self.assertEqual(run.approvals[0].approvers, ["Zoë Example", "Alex Example"])
        self.assertEqual(next(r for r in snapshot.runs if r.id == 12349).state, "inProgress")
        self.assertEqual((snapshot.approvals_error, snapshot.stages_error), ("", ""))

    def test_approval_stages_are_kept_on_disk(self):
        first = FakeClient(builds=[waiting_build()], approvals=[APPROVAL])
        self.store(first).runs("o", "p", "30d", 300)
        self.assertEqual(first.timeline_calls, 1)
        self.assertTrue((self.cache_dir / "approval-stages.json").exists())

        second = FakeClient(builds=[waiting_build()], approvals=[APPROVAL])
        run = self.waiting_run(self.store(second).runs("o", "p", "30d", 300))
        self.assertEqual(run.approvals[0].stage, "Deploy to Staging")
        self.assertEqual(second.timeline_calls, 0)

    def test_approvals_of_runs_outside_the_window_are_ignored(self):
        client = FakeClient(builds=[pr_build()], approvals=[APPROVAL])
        snapshot = self.store(client).runs("o", "p", "30d", 300)
        self.assertEqual([r.state for r in snapshot.runs], ["inProgress"])
        self.assertEqual(client.timeline_calls, 0)

    def test_unreadable_approvals_are_reported_and_runs_stay_running(self):
        client = FakeClient(builds=[waiting_build()], approvals_error=AdoError("HTTP 403: no access", 403))
        snapshot = self.store(client).runs("o", "p", "30d", 300)
        self.assertEqual(self.waiting_run(snapshot).state, "inProgress")
        self.assertIn("no access", snapshot.approvals_error)

    def test_unreadable_timeline_still_marks_the_run_waiting(self):
        client = FakeClient(builds=[waiting_build()], approvals=[APPROVAL], timelines={})
        snapshot = self.store(client).runs("o", "p", "30d", 300)
        run = self.waiting_run(snapshot)
        self.assertEqual((run.state, run.approvals[0].stage), ("waitingForApproval", ""))
        self.assertIn("not found", snapshot.stages_error)

    def test_concurrent_requests_share_one_load(self):
        client = FakeClient()
        slow = client.list_builds

        def list_builds(*args):
            time.sleep(0.2)
            return slow(*args)

        client.list_builds = list_builds
        store = self.store(client)
        threads = [threading.Thread(target=store.runs, args=("o", "p", "30d", 300)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(client.build_calls, 1)


if __name__ == "__main__":
    unittest.main()
