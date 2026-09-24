import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from runs import (
    DEFAULT_SORT,
    View,
    apply_approvals,
    apply_merge_commit,
    approval_stages,
    filter_runs,
    format_ago,
    format_duration,
    format_elapsed,
    match_terms,
    needs_merge_commit,
    normalize_build,
    parse_approval,
    parse_pr_merge_comment,
    parse_sort,
    parse_terms,
    short_branch,
    sort_runs,
    toggle_sort,
)
from tests.samples import (
    APPROVAL,
    APPROVAL_ID,
    CI_SHA,
    PR_MERGE_COMMIT,
    PR_MERGE_SHA,
    PR_SOURCE_SHA,
    TIMELINE,
    ci_build,
    pr_build,
    waiting_build,
)

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def resolved_pr(**overrides):
    run = normalize_build(pr_build(**overrides))
    apply_merge_commit(run, PR_MERGE_COMMIT["parents"][1], PR_MERGE_COMMIT["comment"])
    return run


def ids(runs):
    return [r.id for r in runs]


class NormalizeTest(unittest.TestCase):
    def test_ci_build(self):
        r = normalize_build(ci_build())
        self.assertEqual(r.state, "succeeded")
        self.assertEqual(r.reason_group, "ci")
        self.assertEqual(r.folder, "orders-api")
        self.assertEqual(r.branch, "main")
        self.assertEqual(r.display_sha, CI_SHA)
        self.assertEqual(r.duration(NOW), 210)
        self.assertEqual(
            r.pipeline_url,
            "https://dev.azure.com/fabrikam/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/_build?definitionId=305",
        )
        self.assertEqual(
            r.commit_url(CI_SHA),
            f"https://dev.azure.com/fabrikam/Fabrikam%20Fiber/_git/FabrikamPlatform/commit/{CI_SHA}",
        )
        self.assertFalse(needs_merge_commit(r))

    def test_pr_build_before_and_after_merge_commit_lookup(self):
        r = normalize_build(pr_build())
        self.assertEqual((r.branch, r.pr_number, r.state), ("PR 10206", "10206", "inProgress"))
        self.assertEqual(r.display_sha, PR_MERGE_SHA)
        self.assertTrue(needs_merge_commit(r))

        apply_merge_commit(r, PR_SOURCE_SHA, PR_MERGE_COMMIT["comment"])
        self.assertFalse(needs_merge_commit(r))
        self.assertEqual(r.display_sha, PR_SOURCE_SHA)
        self.assertEqual((r.pr_source_branch, r.pr_target_branch), ("feature/order-export", "develop"))
        self.assertEqual(r.pr_url, "https://dev.azure.com/fabrikam/Fabrikam%20Fiber/_git/FabrikamPlatform/pullrequest/10206")
        self.assertIsNone(r.finish_time)
        self.assertEqual(r.duration(NOW), (NOW - r.start_time).total_seconds())

    def test_single_parent_merge_commit_is_not_looked_up_again(self):
        r = normalize_build(pr_build())
        apply_merge_commit(r, "", "some commit")
        self.assertFalse(needs_merge_commit(r))
        self.assertEqual(r.display_sha, PR_MERGE_SHA)

    def test_states_are_grouped(self):
        self.assertEqual(normalize_build(ci_build(status="cancelling", result=None)).state, "canceled")
        self.assertEqual(normalize_build(ci_build(status="postponed", result=None)).state, "notStarted")
        self.assertEqual(normalize_build(ci_build(result="failed")).state, "failed")

    def test_unset_times_and_non_https_links_are_dropped(self):
        r = normalize_build(
            ci_build(startTime="0001-01-01T00:00:00", _links={"web": {"href": "javascript:alert(1)"}})
        )
        self.assertIsNone(r.start_time)
        self.assertIsNone(r.duration(NOW))
        self.assertEqual((r.url, r.pipeline_url), ("", ""))

    def test_short_branch_and_merge_comment(self):
        self.assertEqual(short_branch("refs/heads/feature/x"), "feature/x")
        self.assertEqual(short_branch("refs/tags/v1.2"), "tags/v1.2")
        self.assertEqual(short_branch("refs/pull/7/merge"), "PR 7")
        self.assertEqual(parse_pr_merge_comment("Merge pull request 7 from a/b into main\n\nbody"), ("a/b", "main"))
        self.assertIsNone(parse_pr_merge_comment("Fix the thing"))


class TermsTest(unittest.TestCase):
    def test_parse(self):
        terms = parse_terms(" Main , -Build Service, !bot, , - ")
        self.assertEqual([(t.value, t.negate) for t in terms], [("main", False), ("build service", True), ("bot", True)])

    def test_match(self):
        def contains(text):
            return lambda v: v in text

        self.assertTrue(match_terms(parse_terms("a, b"), contains("xbx")))
        self.assertFalse(match_terms(parse_terms("a, b"), contains("xcx")))
        self.assertTrue(match_terms(parse_terms("-a"), contains("xcx")))
        self.assertFalse(match_terms(parse_terms("c, -x"), contains("xcx")))


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.ci = normalize_build(ci_build())
        self.other_ci = normalize_build(
            ci_build(
                id=12300,
                sourceVersion="aaaa" + CI_SHA[4:],
                triggerInfo={"ci.sourceSha": "aaaa" + CI_SHA[4:], "ci.message": "Bump version"},
                result="failed",
            )
        )
        self.pr = resolved_pr()
        self.runs = [self.ci, self.other_ci, self.pr]

    def show(self, **filters):
        return ids(filter_runs(self.runs, View(**filters)))

    def test_commit_is_a_sha_prefix(self):
        self.assertEqual(self.show(commit="c0ffee5"), [12363])
        self.assertEqual(self.show(commit="C0FFEE5A1B2C"), [12363])
        self.assertEqual(self.show(commit="ffee5a"), [])  # not a prefix

    def test_commit_matches_the_pushed_commit_of_a_pr_build(self):
        self.assertEqual(self.show(commit=PR_SOURCE_SHA[:7]), [12349])
        self.assertEqual(self.show(commit=PR_MERGE_SHA[:7]), [12349])

    def test_commit_matches_message_text_and_several_terms(self):
        self.assertEqual(self.show(commit="pr 10205"), [12363])
        self.assertEqual(self.show(commit="c0ffee5, bump"), [12363, 12300])
        self.assertEqual(self.show(commit="-c0ffee5"), [12300, 12349])

    def test_requested_by_uses_requested_for(self):
        self.assertEqual(self.show(requested_by="zoë"), [12363, 12300])
        self.assertEqual(self.show(requested_by="alex@"), [12349])
        self.assertEqual(self.show(requested_by="-zoë"), [12349])
        # The service identity that queues CI runs is not what the column shows.
        self.assertEqual(self.show(requested_by="VisualStudio.Services"), [])

    def test_pipeline_matches_name_and_folder(self):
        self.assertEqual(self.show(pipeline="storefront"), [12349])
        self.assertEqual(self.show(pipeline="orders-api/orders-a"), [12363, 12300])

    def test_run_matches_number_or_id(self):
        self.assertEqual(self.show(run="12349"), [12349])
        self.assertEqual(self.show(run="1.37"), [12363, 12300])

    def test_branch_matches_pr_branches(self):
        self.assertEqual(self.show(branch="order-export"), [12349])
        self.assertEqual(self.show(branch="main"), [12363, 12300])
        self.assertEqual(self.show(branch="PR 10206"), [12349])

    def test_state_and_reason(self):
        self.assertEqual(self.show(states=("failed", "inProgress")), [12300, 12349])
        self.assertEqual(self.show(reasons=("pr",)), [12349])
        self.assertEqual(self.show(reasons=("ci",), states=("succeeded",)), [12363])


class SortTest(unittest.TestCase):
    def setUp(self):
        self.a = normalize_build(ci_build(id=1, definition={"id": 1, "name": "beta"}, queueTime="2026-09-20T10:00:00Z"))
        self.b = normalize_build(ci_build(id=2, definition={"id": 2, "name": "alpha"}, queueTime="2026-09-21T10:00:00Z"))
        self.c = normalize_build(ci_build(id=3, definition={"id": 1, "name": "Beta"}, queueTime="2026-09-22T10:00:00Z"))
        self.queued = normalize_build(
            ci_build(id=4, definition={"id": 3, "name": "gamma"}, status="notStarted", startTime=None, finishTime=None)
        )
        self.runs = [self.a, self.b, self.c, self.queued]

    def sorted_ids(self, sort):
        return ids(sort_runs(self.runs, sort, NOW))

    def test_default_is_newest_queued_first(self):
        self.assertEqual(self.sorted_ids(DEFAULT_SORT), [4, 3, 2, 1])

    def test_secondary_key(self):
        self.assertEqual(self.sorted_ids((("pipeline", "asc"), ("queued", "desc"))), [2, 3, 1, 4])
        self.assertEqual(self.sorted_ids((("pipeline", "asc"), ("queued", "asc"))), [2, 1, 3, 4])

    def test_missing_values_go_last_both_ways(self):
        self.assertEqual(self.sorted_ids((("duration", "desc"),))[-1], 4)
        self.assertEqual(self.sorted_ids((("duration", "asc"),))[-1], 4)

    def test_ties_end_with_newest_id(self):
        # a ("beta") and c ("Beta") tie on both keys.
        self.assertEqual(self.sorted_ids((("duration", "asc"), ("pipeline", "asc"))), [2, 3, 1, 4])

    def test_toggle(self):
        self.assertEqual(toggle_sort(DEFAULT_SORT, "queued", False), (("queued", "asc"),))
        self.assertEqual(toggle_sort(DEFAULT_SORT, "pipeline", False), (("pipeline", "asc"),))
        self.assertEqual(toggle_sort(DEFAULT_SORT, "duration", False), (("duration", "desc"),))
        both = toggle_sort((("pipeline", "asc"),), "queued", True)
        self.assertEqual(both, (("pipeline", "asc"), ("queued", "desc")))
        self.assertEqual(toggle_sort(both, "queued", True), (("pipeline", "asc"), ("queued", "asc")))
        self.assertEqual(toggle_sort(both, "pipeline", False), (("pipeline", "desc"),))

    def test_parse_sort_drops_unknown_and_repeated_keys(self):
        self.assertEqual(parse_sort("pipeline:asc,nope:asc,pipeline:desc,queued"), (("pipeline", "asc"), ("queued", "desc")))


class ViewTest(unittest.TestCase):
    def test_round_trip(self):
        view = View(
            org="fabrikam",
            project="Fabrikam Fiber",
            window="7d",
            pipeline="orders, -web",
            requested_by="marko",
            commit="c0ffee5",
            branch="main",
            states=("failed", "canceled"),
            reasons=("pr",),
            sort=(("pipeline", "asc"), ("queued", "desc")),
            auto=True,
        )
        self.assertEqual(View.from_query(view.to_query()), view)

    def test_defaults_are_left_out_of_the_query(self):
        self.assertEqual(View(org="o", project="p").to_query(), "org=o&project=p")

    def test_invalid_values_fall_back(self):
        v = View.from_query("window=1y&state=bogus&state=failed&reason=x&sort=nope:asc&by=%20a%20")
        self.assertEqual((v.window, v.states, v.reasons, v.sort, v.requested_by), ("30d", ("failed",), (), DEFAULT_SORT, "a"))

    def test_filters_cleared_keeps_scope_and_sort(self):
        v = View(org="o", project="p", window="7d", commit="x", states=("failed",), sort=(("run", "asc"),))
        self.assertEqual(v.filters_cleared(), replace(v, commit="", states=()))
        self.assertFalse(v.filters_cleared().has_filters)


class ApprovalTest(unittest.TestCase):
    def test_parse_approval_keeps_only_approvers_still_to_respond(self):
        build_id, approval = parse_approval(APPROVAL)
        self.assertEqual(build_id, 12363)
        self.assertEqual(approval.id, APPROVAL_ID)
        self.assertEqual(approval.approvers, ["Zoë Example", "Alex Example"])
        self.assertEqual(approval.min_required, 1)
        self.assertEqual(approval.created_on, datetime(2026, 9, 24, 7, 54, 41, 507000, tzinfo=timezone.utc))

    def test_parse_approval_needs_a_build(self):
        self.assertIsNone(parse_approval({"id": "x", "pipeline": {"owner": {}}}))

    def test_approval_stages_walk_up_to_the_stage(self):
        stages = approval_stages(TIMELINE)
        self.assertEqual(stages[APPROVAL_ID], "Deploy to Staging")
        self.assertEqual(stages["0a0a0a0a-0000-4000-8000-000000000003"], "Deploy to Demo")

    def test_approval_stages_survive_broken_parent_chains(self):
        loop = [
            {"id": "a", "type": "Checkpoint.Approval", "parentId": "b"},
            {"id": "b", "type": "Checkpoint", "parentId": "c"},
            {"id": "c", "type": "Checkpoint", "parentId": "b"},
            {"id": "d", "type": "Checkpoint.Approval", "parentId": "missing"},
        ]
        self.assertEqual(approval_stages(loop), {})

    def test_waiting_state_replaces_running_only(self):
        _, approval = parse_approval(APPROVAL)
        waiting = normalize_build(waiting_build())
        apply_approvals(waiting, [approval])
        self.assertEqual(waiting.state, "waitingForApproval")
        self.assertEqual(waiting.waiting_since, approval.created_on)

        finished = normalize_build(ci_build())
        apply_approvals(finished, [approval])
        self.assertEqual((finished.state, finished.approvals), ("succeeded", []))

    def test_filter_and_query_know_the_waiting_state(self):
        _, approval = parse_approval(APPROVAL)
        waiting = normalize_build(waiting_build())
        apply_approvals(waiting, [approval])
        running = normalize_build(pr_build())
        runs = [waiting, running]
        self.assertEqual(ids(filter_runs(runs, View(states=("waitingForApproval",)))), [12363])
        self.assertEqual(ids(filter_runs(runs, View(states=("inProgress",)))), [12349])
        view = View.from_query("state=waitingForApproval&state=inProgress")
        self.assertEqual(view.states, ("waitingForApproval", "inProgress"))


class FormatTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual([format_duration(s) for s in (None, 45, 192, 3840)], ["", "45s", "3m 12s", "1h 04m"])

    def test_elapsed_and_ago(self):
        self.assertEqual(format_elapsed(NOW - timedelta(seconds=20), NOW), "")
        self.assertEqual(format_elapsed(NOW - timedelta(minutes=12), NOW), "12 min")
        self.assertEqual(format_elapsed(NOW - timedelta(hours=3), NOW), "3 h")
        self.assertEqual(format_elapsed(NOW - timedelta(days=4), NOW), "4 days")
        self.assertEqual(format_ago(NOW - timedelta(seconds=20), NOW), "just now")
        self.assertEqual(format_ago(NOW - timedelta(hours=3), NOW), "3 h ago")


if __name__ == "__main__":
    unittest.main()
