import threading

from ado import AdoError
from tests.samples import PR_MERGE_COMMIT, PR_MERGE_SHA, REPO_ID, TIMELINE, ci_build, pr_build


class FakeClient:
    """Stands in for AdoClient; counts calls so tests can see what was fetched."""

    def __init__(self, builds=None, commits=None, error=None, approvals=None, timelines=None, approvals_error=None):
        self.builds = builds if builds is not None else [ci_build(), pr_build()]
        self.commits = commits if commits is not None else {(REPO_ID, PR_MERGE_SHA): PR_MERGE_COMMIT}
        self.approvals = approvals or []
        self.timelines = timelines if timelines is not None else {12363: TIMELINE}
        self.error = error
        self.approvals_error = approvals_error
        self.build_calls = 0
        self.commit_calls = 0
        self.timeline_calls = 0
        self._lock = threading.Lock()

    def list_builds(self, org, project, min_time):
        with self._lock:
            self.build_calls += 1
        if self.error:
            raise self.error
        return list(self.builds), False

    def get_commit(self, org, repo_id, sha):
        with self._lock:
            self.commit_calls += 1
        try:
            return self.commits[(repo_id, sha)]
        except KeyError:
            raise AdoError(f"commit {sha} not found", 404) from None

    def list_pending_approvals(self, org, project):
        if self.approvals_error:
            raise self.approvals_error
        return list(self.approvals)

    def get_timeline(self, org, project, build_id):
        with self._lock:
            self.timeline_calls += 1
        try:
            return self.timelines[build_id]
        except KeyError:
            raise AdoError(f"build {build_id} not found", 404) from None

    def list_projects(self, org):
        return ["Fabrikam Fiber", "Other"]
