"""Pure run-list logic: normalising builds, filtering, sorting, formatting and
the view state carried in the query string. No I/O happens here."""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode

STATES = (
    "succeeded",
    "partiallySucceeded",
    "failed",
    "canceled",
    "waitingForApproval",
    "inProgress",
    "notStarted",
)
STATE_LABELS = {
    "succeeded": "Succeeded",
    "partiallySucceeded": "Partially succeeded",
    "failed": "Failed",
    "canceled": "Canceled",
    "waitingForApproval": "Waiting for approval",
    "inProgress": "Running",
    "notStarted": "Queued",
}
# Statuses/results that share a filter checkbox with one of STATES.
_STATE_GROUP = {"cancelling": "canceled", "postponed": "notStarted"}
# Azure DevOps reports a run held at an approval as in progress; the pending
# approvals are read separately and turn these states into waitingForApproval.
_WAITABLE_STATES = {"inProgress", "notStarted"}

REASONS = ("ci", "pr", "trigger", "manual", "schedule", "other")
REASON_LABELS = {
    "ci": "CI",
    "pr": "Pull request",
    "trigger": "Pipeline trigger",
    "manual": "Manual",
    "schedule": "Scheduled",
    "other": "Other",
}
_REASON_GROUP = {
    "individualCI": "ci",
    "batchedCI": "ci",
    "pullRequest": "pr",
    "buildCompletion": "trigger",
    "resourceTrigger": "trigger",
    "manual": "manual",
    "schedule": "schedule",
    "scheduleForced": "schedule",
}

_DAY = 24 * 60 * 60
WINDOWS = {"24h": _DAY, "7d": 7 * _DAY, "30d": 30 * _DAY, "90d": 90 * _DAY, "all": None}
WINDOW_LABELS = {
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "all": "All retained runs",
}
DEFAULT_WINDOW = "30d"

SORT_KEYS = ("state", "pipeline", "run", "commit", "branch", "reason", "by", "queued", "duration")
DEFAULT_SORT = (("queued", "desc"),)
# Columns whose first click should show the newest / biggest first.
_DESC_FIRST = {"run", "queued", "duration"}

_PR_REF = re.compile(r"^refs/pull/(\d+)/")
# Azure Repos names its PR merge commits "Merge pull request 123 from <source> into <target>".
_PR_MERGE_COMMENT = re.compile(r"^Merge pull request \d+ from (.+) into (.+?)\s*$")


def short_branch(ref: str) -> str:
    if not ref:
        return ""
    pr = _PR_REF.match(ref)
    if pr:
        return f"PR {pr.group(1)}"
    for prefix in ("refs/heads/", "refs/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def parse_pr_merge_comment(comment: str) -> tuple[str, str] | None:
    m = _PR_MERGE_COMMENT.match((comment or "").split("\n", 1)[0])
    return (m.group(1), m.group(2)) if m else None


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    # ADO uses 0001-01-01 for "not set".
    return t if t.year > 1 else None


def _https(url: str) -> str:
    return url if url.startswith("https://") else ""


@dataclass
class Approval:
    """A pending approval that holds a run at the start of a stage."""

    id: str
    created_on: datetime | None = None
    # Display names of the approvers who have not responded yet (people or groups).
    approvers: list[str] = field(default_factory=list)
    min_required: int = 0
    stage: str = ""


@dataclass
class Run:
    id: int
    build_number: str = ""
    url: str = ""
    pipeline: str = ""
    folder: str = ""
    pipeline_url: str = ""
    state: str = ""
    reason: str = ""
    reason_group: str = "other"
    branch_ref: str = ""
    branch: str = ""
    pr_number: str = ""
    pr_source_branch: str = ""
    pr_target_branch: str = ""
    # For PR builds `sha` is the merge commit ADO creates; the commit that was
    # pushed is its second parent, filled in by apply_merge_commit.
    sha: str = ""
    ci_sha: str = ""
    pr_source_sha: str = ""
    merge_resolved: bool = False
    message: str = ""
    requested_for: str = ""
    requested_for_id: str = ""
    requested_by: str = ""
    queue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    tags: list[str] = field(default_factory=list)
    repo_type: str = ""
    repo_id: str = ""
    repo_url: str = ""
    approvals: list[Approval] = field(default_factory=list)

    @property
    def waiting_since(self) -> datetime | None:
        times = [a.created_on for a in self.approvals if a.created_on]
        return min(times) if times else None

    @property
    def display_sha(self) -> str:
        """The commit a person recognises: a PR's pushed commit, else the built one."""
        return self.pr_source_sha or self.ci_sha or self.sha

    def duration(self, now: datetime) -> float | None:
        if not self.start_time:
            return None
        return ((self.finish_time or now) - self.start_time).total_seconds()

    def commit_url(self, sha: str) -> str:
        if not sha or not self.repo_url or self.repo_type not in ("TfsGit", "GitHub"):
            return ""
        return f"{self.repo_url}/commit/{sha}"

    @property
    def pr_url(self) -> str:
        if not self.pr_number or not self.repo_url:
            return ""
        if self.repo_type == "TfsGit":
            return f"{self.repo_url}/pullrequest/{self.pr_number}"
        if self.repo_type == "GitHub":
            return f"{self.repo_url}/pull/{self.pr_number}"
        return ""


def normalize_build(b: dict) -> Run:
    """Flattens a build from GET _apis/build/builds."""
    ti = b.get("triggerInfo") or {}
    repo = b.get("repository") or {}
    definition = b.get("definition") or {}
    ref = b.get("sourceBranch") or ""
    status = b.get("status") or "none"
    raw_state = (b.get("result") or "none") if status == "completed" else status
    web = _https(((b.get("_links") or {}).get("web") or {}).get("href") or "")
    pipeline_url = ""
    if web and definition.get("id"):
        pipeline_url = re.sub(r"/_build/results\?.*$", f"/_build?definitionId={definition['id']}", web)
    pr = _PR_REF.match(ref)
    requested_for = b.get("requestedFor") or {}
    return Run(
        id=b["id"],
        build_number=b.get("buildNumber") or "",
        url=web,
        pipeline=definition.get("name") or "",
        folder=(definition.get("path") or "").lstrip("\\"),
        pipeline_url=pipeline_url,
        state=_STATE_GROUP.get(raw_state, raw_state),
        reason=b.get("reason") or "",
        reason_group=_REASON_GROUP.get(b.get("reason"), "other"),
        branch_ref=ref,
        branch=short_branch(ref),
        pr_number=str(ti.get("pr.number") or (pr.group(1) if pr else "")),
        pr_source_branch=short_branch(ti.get("pr.sourceBranch") or ""),
        pr_target_branch=short_branch(ti.get("pr.targetBranch") or ""),
        sha=b.get("sourceVersion") or "",
        ci_sha=ti.get("ci.sourceSha") or "",
        pr_source_sha=ti.get("pr.sourceSha") or "",
        message=ti.get("ci.message") or "",
        requested_for=requested_for.get("displayName") or "",
        requested_for_id=requested_for.get("uniqueName") or "",
        requested_by=(b.get("requestedBy") or {}).get("displayName") or "",
        queue_time=_parse_time(b.get("queueTime")),
        start_time=_parse_time(b.get("startTime")),
        finish_time=_parse_time(b.get("finishTime")),
        tags=list(b.get("tags") or []),
        repo_type=repo.get("type") or "",
        repo_id=repo.get("id") or "",
        repo_url=_https(re.sub(r"\.git$", "", repo.get("url") or "")),
    )


def needs_merge_commit(run: Run) -> bool:
    return (
        run.reason_group == "pr"
        and run.repo_type == "TfsGit"
        and bool(run.sha)
        and not run.pr_source_sha
        and not run.merge_resolved
    )


def apply_merge_commit(run: Run, parent: str, comment: str) -> None:
    """parent: second parent of the PR merge commit; comment: its message."""
    first_line = (comment or "").split("\n", 1)[0]
    branches = parse_pr_merge_comment(first_line)
    run.merge_resolved = True
    run.pr_source_sha = run.pr_source_sha or parent
    run.message = run.message or first_line
    if branches:
        run.pr_source_branch = run.pr_source_branch or branches[0]
        run.pr_target_branch = run.pr_target_branch or branches[1]


def parse_approval(a: dict) -> tuple[int, Approval] | None:
    """(build id, Approval) from GET _apis/pipelines/approvals?$expand=steps."""
    build_id = ((a.get("pipeline") or {}).get("owner") or {}).get("id")
    if not isinstance(build_id, int) or not a.get("id"):
        return None
    approvers = [
        s["assignedApprover"].get("displayName") or ""
        for s in a.get("steps") or []
        if s.get("status") == "pending" and s.get("assignedApprover")
    ]
    return build_id, Approval(
        id=a["id"].casefold(),
        created_on=_parse_time(a.get("createdOn")),
        approvers=[name for name in approvers if name],
        min_required=a.get("minRequiredApprovers") or 0,
    )


def approval_stages(records: list[dict]) -> dict[str, str]:
    """Approval id -> name of the stage it gates, from a build timeline.

    Each approval appears as a Checkpoint.Approval record whose id is the
    approval id, under a Checkpoint record under the Stage record."""
    by_id = {r.get("id"): r for r in records}
    stages = {}
    for r in records:
        if r.get("type") != "Checkpoint.Approval" or not r.get("id"):
            continue
        node = by_id.get(r.get("parentId"))
        seen = set()
        while node is not None and node.get("type") != "Stage" and node.get("id") not in seen:
            seen.add(node.get("id"))
            node = by_id.get(node.get("parentId"))
        if node is not None and node.get("type") == "Stage":
            stages[r["id"].casefold()] = node.get("name") or node.get("identifier") or ""
    return stages


def apply_approvals(run: Run, approvals: list[Approval]) -> None:
    # A finished run can't be held; ignore approvals ADO has not closed yet.
    if not approvals or run.state not in _WAITABLE_STATES:
        return
    run.approvals = sorted(approvals, key=lambda a: a.created_on or datetime.max.replace(tzinfo=timezone.utc))
    run.state = "waitingForApproval"


# ---- Filtering -------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    value: str
    negate: bool = False


def parse_terms(text: str) -> list[Term]:
    """'a, b, -c' -> wanted a or b, excluding c. A leading '!' also excludes."""
    terms = []
    for part in (text or "").split(","):
        v = part.strip()
        negate = v[:1] in ("-", "!")
        if negate:
            v = v[1:].strip()
        if v:
            terms.append(Term(v.casefold(), negate))
    return terms


def match_terms(terms: list[Term], test) -> bool:
    """No excluded term may match; if there are wanted terms, one must match."""
    wanted = found = False
    for t in terms:
        hit = test(t.value)
        if t.negate:
            if hit:
                return False
        else:
            wanted = True
            found = found or hit
    return found or not wanted


def _match_pipeline(r: Run, v: str) -> bool:
    return v.replace("/", "\\") in f"{r.folder}\\{r.pipeline}".casefold()


def _match_run(r: Run, v: str) -> bool:
    return v in r.build_number.casefold() or v == str(r.id)


def _match_requested_by(r: Run, v: str) -> bool:
    return v in r.requested_for.casefold() or v in r.requested_for_id.casefold()


def _match_commit(r: Run, v: str) -> bool:
    shas = (r.sha, r.ci_sha, r.pr_source_sha)
    return any(s.casefold().startswith(v) for s in shas if s) or v in r.message.casefold()


def _match_branch(r: Run, v: str) -> bool:
    names = (r.branch, r.branch_ref, r.pr_source_branch, r.pr_target_branch)
    return any(v in s.casefold() for s in names)


_TEXT_MATCHERS = {
    "pipeline": _match_pipeline,
    "run": _match_run,
    "requested_by": _match_requested_by,
    "commit": _match_commit,
    "branch": _match_branch,
}
TEXT_FILTERS = tuple(_TEXT_MATCHERS)


@dataclass(frozen=True)
class View:
    """Everything that decides what the page shows; round-trips through the query string."""

    org: str = ""
    project: str = ""
    window: str = DEFAULT_WINDOW
    pipeline: str = ""
    run: str = ""
    requested_by: str = ""
    commit: str = ""
    branch: str = ""
    states: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    sort: tuple[tuple[str, str], ...] = DEFAULT_SORT
    auto: bool = False

    def filters_cleared(self) -> "View":
        return replace(self, **{k: "" for k in TEXT_FILTERS}, states=(), reasons=())

    @property
    def has_filters(self) -> bool:
        return any(getattr(self, k) for k in TEXT_FILTERS) or bool(self.states or self.reasons)

    # Query parameter names that differ from the field name (not a dataclass
    # field: it has no annotation).
    _PARAMS = {"requested_by": "by"}

    @classmethod
    def from_query(cls, query: str) -> "View":
        q = parse_qs(query or "")

        def one(name: str) -> str:
            return (q.get(name) or [""])[0].strip()

        window = one("window")
        return cls(
            org=one("org"),
            project=one("project"),
            window=window if window in WINDOWS else DEFAULT_WINDOW,
            **{k: one(cls._PARAMS.get(k, k)) for k in TEXT_FILTERS},
            states=tuple(s for s in STATES if s in q.get("state", [])),
            reasons=tuple(s for s in REASONS if s in q.get("reason", [])),
            sort=parse_sort(one("sort")) or DEFAULT_SORT,
            auto=one("auto") == "1",
        )

    def to_query(self, **changes) -> str:
        v = replace(self, **changes)
        params: list[tuple[str, str]] = [("org", v.org), ("project", v.project)]
        if v.window != DEFAULT_WINDOW:
            params.append(("window", v.window))
        params += [(self._PARAMS.get(k, k), getattr(v, k)) for k in TEXT_FILTERS if getattr(v, k)]
        params += [("state", s) for s in v.states]
        params += [("reason", s) for s in v.reasons]
        if v.sort != DEFAULT_SORT:
            params.append(("sort", format_sort(v.sort)))
        if v.auto:
            params.append(("auto", "1"))
        return urlencode(params)


def filter_runs(runs: list[Run], view: View) -> list[Run]:
    text = [(_TEXT_MATCHERS[k], parse_terms(getattr(view, k))) for k in TEXT_FILTERS]
    text = [(match, terms) for match, terms in text if terms]
    states, reasons = set(view.states), set(view.reasons)
    return [
        r
        for r in runs
        if (not states or r.state in states)
        and (not reasons or r.reason_group in reasons)
        and all(match_terms(terms, lambda v, m=match: m(r, v)) for match, terms in text)
    ]


# ---- Sorting ---------------------------------------------------------------


def parse_sort(text: str) -> tuple[tuple[str, str], ...]:
    keys: list[tuple[str, str]] = []
    for part in (text or "").split(","):
        key, _, direction = part.strip().partition(":")
        if key in SORT_KEYS and key not in (k for k, _ in keys):
            keys.append((key, "asc" if direction == "asc" else "desc"))
    return tuple(keys)


def format_sort(sort) -> str:
    return ",".join(f"{k}:{d}" for k, d in sort)


def _rank(order, value) -> int:
    return order.index(value) if value in order else len(order)


_SORT_VALUE = {
    "state": lambda r, now: _rank(STATES, r.state),
    "pipeline": lambda r, now: r.pipeline.casefold() or None,
    "run": lambda r, now: r.id,
    "commit": lambda r, now: r.display_sha.casefold() or None,
    "branch": lambda r, now: r.branch.casefold() or None,
    "reason": lambda r, now: _rank(REASONS, r.reason_group),
    "by": lambda r, now: r.requested_for.casefold() or None,
    "queued": lambda r, now: r.queue_time,
    "duration": lambda r, now: r.duration(now),
}


def sort_runs(runs: list[Run], sort, now: datetime) -> list[Run]:
    """Sorts by each (key, dir) in priority order; missing values go last in
    either direction, and ties end with the newest run id first."""
    result = sorted(runs, key=lambda r: r.id, reverse=True)
    # Stable sorts applied from the least to the most significant key.
    for key, direction in reversed(sort):
        value = _SORT_VALUE[key]
        present = [r for r in result if value(r, now) is not None]
        missing = [r for r in result if value(r, now) is None]
        present.sort(key=lambda r: value(r, now), reverse=direction == "desc")
        result = present + missing
    return result


def toggle_sort(sort, key: str, additive: bool):
    """A plain click sorts by the column alone and flips it on repeat; an
    additive click appends the column as the next key, or flips it in place."""
    flip = {"asc": "desc", "desc": "asc"}
    first = "desc" if key in _DESC_FIRST else "asc"
    if not additive:
        return ((key, flip[sort[0][1]] if sort and sort[0][0] == key else first),)
    if any(k == key for k, _ in sort):
        return tuple((k, flip[d] if k == key else d) for k, d in sort)
    return (*sort, (key, first))


# ---- Formatting ------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    return f"{m // 60}h {m % 60:02d}m"


def format_datetime(t: datetime | None) -> str:
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else ""


def format_elapsed(t: datetime | None, now: datetime) -> str:
    """Rough time since t: '12 min', '3 h', '2 days'; '' for under a minute."""
    if not t:
        return ""
    minutes = round((now - t).total_seconds() / 60)
    if minutes < 1:
        return ""
    if minutes < 60:
        return f"{minutes} min"
    hours = round(minutes / 60)
    if hours < 48:
        return f"{hours} h"
    return f"{round(hours / 24)} days"


def format_ago(t: datetime | None, now: datetime) -> str:
    if not t:
        return ""
    elapsed = format_elapsed(t, now)
    return f"{elapsed} ago" if elapsed else "just now"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
