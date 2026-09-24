"""Renders the runs page as plain HTML. Every value from Azure DevOps or the
query string is escaped, and the page contains no script."""

import html
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from runs import (
    REASON_LABELS,
    REASONS,
    STATE_LABELS,
    STATES,
    WINDOW_LABELS,
    Run,
    View,
    filter_runs,
    format_ago,
    format_datetime,
    format_duration,
    format_elapsed,
    format_sort,
    sort_runs,
    toggle_sort,
)

AUTO_REFRESH_SECONDS = 30
_TEMPLATE = Template((Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8"))

COLUMNS = (
    ("state", "State"),
    ("pipeline", "Pipeline"),
    ("run", "Run"),
    ("commit", "Commit"),
    ("branch", "Branch"),
    ("reason", "Reason"),
    ("by", "Requested by"),
    ("queued", "Queued"),
    ("duration", "Duration"),
)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def _attrs(cls: str = "", title: str = "") -> str:
    return (f' class="{esc(cls)}"' if cls else "") + (f' title="{esc(title)}"' if title else "")


def _external(text: str, href: str, cls: str = "", title: str = "") -> str:
    """A link into Azure DevOps (hrefs are https-only by construction in runs.py)."""
    if not href:
        return f"<span{_attrs(cls, title)}>{esc(text)}</span>"
    return f'<a href="{esc(href)}"{_attrs(cls, title)} target="_blank" rel="noopener noreferrer">{esc(text)}</a>'


def _here(text: str, query: str, cls: str = "", title: str = "") -> str:
    """A link to this page with another view."""
    return f'<a href="?{esc(query)}"{_attrs(cls, title)}>{esc(text)}</a>'


def _head(view: View) -> str:
    position = {key: i for i, (key, _) in enumerate(view.sort)}
    multi = len(view.sort) > 1
    cells = []
    for key, label in COLUMNS:
        pos = position.get(key)
        sort_alone = view.to_query(sort=toggle_sort(view.sort, key, additive=False))
        sort_then = view.to_query(sort=toggle_sort(view.sort, key, additive=True))
        aria = ""
        if pos is None:
            extra = _here("+", sort_then, "then", f"Then sort by {label}")
        else:
            direction = view.sort[pos][1]
            arrow = ("▲" if direction == "asc" else "▼") + (str(pos + 1) if multi else "")
            if pos == 0:
                extra = f'<span class="arrow">{arrow}</span>'
                aria = f' aria-sort="{"ascending" if direction == "asc" else "descending"}"'
            else:
                extra = _here(arrow, sort_then, "then on", "Reverse this sort key")
        main = _here(label, sort_alone, "sort", f"Sort by {label}")
        cells.append(f'<th class="c-{key}"{aria}>{main}{extra}</th>')
    return "".join(cells)


def _commit_cell(r: Run, view: View) -> str:
    sha = r.display_sha
    if not sha:
        return ""
    pushed = r.pr_source_sha and r.pr_source_sha != r.sha
    title = f"Pushed commit {r.pr_source_sha}\nBuilt as merge commit {r.sha}" if pushed else sha
    parts = [_external(sha[:8], r.commit_url(sha), "sha", title)]
    if pushed:
        parts.append('<span class="badge" title="The commit pushed to the pull request. '
                     'Azure DevOps built a merge commit of it into the target branch.">PR</span>')
    parts.append(_here("all runs", view.filters_cleared().to_query(commit=sha), "only",
                       "Show every run of this commit"))
    message = f'<div class="sub msg"{_attrs(title=r.message)}>{esc(r.message)}</div>' if r.message else ""
    return " ".join(parts) + message


def _branch_cell(r: Run) -> str:
    if r.pr_number:
        cell = _external(f"PR {r.pr_number}", r.pr_url, title=r.branch_ref)
        if r.pr_source_branch and r.pr_target_branch:
            cell += f'<div class="sub">{esc(r.pr_source_branch)} → {esc(r.pr_target_branch)}</div>'
        return cell
    return f"<span{_attrs(title=r.branch_ref)}>{esc(r.branch)}</span>"


def _state_cell(r: Run, now: datetime) -> str:
    text = STATE_LABELS.get(r.state, r.state)
    if not r.approvals:
        return f'<td class="c-state"><span class="dot"></span>{esc(text)}</td>'
    # Approving happens on the run's page in Azure DevOps (its Review button).
    label = f'<span class="dot"></span>{_external(text, r.url, "approve")}'
    stages = ", ".join(dict.fromkeys(a.stage for a in r.approvals if a.stage))
    waited = format_elapsed(r.waiting_since, now)
    sub = " · ".join(part for part in (stages, f"waiting {waited}" if waited else "") if part)
    lines = [f"Waiting since {format_datetime(r.waiting_since)}"] if r.waiting_since else []
    for a in r.approvals:
        needed = f" ({a.min_required} approval{'s' if a.min_required > 1 else ''} needed)" if a.min_required else ""
        lines.append(f"{a.stage or 'Approval'}: {', '.join(a.approvers) or 'no approver has to respond'}{needed}")
    title = "\n".join(lines)
    return f'<td class="c-state"{_attrs(title=title)}>{label}<div class="sub">{esc(sub)}</div></td>'


def _row(r: Run, view: View, now: datetime) -> str:
    folder = f'<div class="sub">{esc(r.folder)}</div>' if r.folder else ""
    tags = "".join(f' <span class="tag">{esc(t)}</span>' for t in r.tags)
    queued_by = f"Queued by {r.requested_by}" if r.requested_by and r.requested_by != r.requested_for else ""
    requested_title = "\n".join(t for t in (r.requested_for_id, queued_by) if t)
    running = r.start_time is not None and r.finish_time is None
    cells = (
        _state_cell(r, now),
        f'<td class="c-pipeline">{_external(r.pipeline, r.pipeline_url)}{folder}</td>',
        f'<td class="c-run">{_external(r.build_number or str(r.id), r.url)}<div class="sub">#{r.id}{tags}</div></td>',
        f'<td class="c-commit">{_commit_cell(r, view)}</td>',
        f'<td class="c-branch">{_branch_cell(r)}</td>',
        f'<td class="c-reason"{_attrs(title=r.reason)}>{esc(REASON_LABELS.get(r.reason_group, r.reason))}</td>',
        f'<td class="c-by"{_attrs(title=requested_title)}>{esc(r.requested_for)}</td>',
        f'<td class="c-queued"{_attrs(title=format_ago(r.queue_time, now))}>{esc(format_datetime(r.queue_time))}</td>',
        f'<td class="c-duration"{_attrs("live" if running else "", "Still running" if running else "")}>'
        f"{esc(format_duration(r.duration(now)))}</td>",
    )
    return f'<tr class="s-{esc(r.state)}">{"".join(cells)}</tr>'


def _checks(name: str, values, labels: dict, selected, counts: Counter) -> str:
    items = []
    for v in values:
        checked = " checked" if v in selected else ""
        items.append(
            f'<label class="chip {name}-{v}"><input type="checkbox" name="{name}" value="{v}"{checked}>'
            f'<span>{esc(labels[v])}</span><span class="n">{counts.get(v, 0)}</span></label>'
        )
    return "\n      ".join(items)


def _datalist(list_id: str, values) -> str:
    options = "".join(f'<option value="{esc(v)}">' for v in sorted({v for v in values if v}, key=str.casefold))
    return f'<datalist id="{list_id}">{options}</datalist>'


def render_page(view: View, snapshot, error: str, projects: list[str], auth_mode: str, now: datetime) -> str:
    runs = snapshot.runs if snapshot else []
    shown = sort_runs(filter_runs(runs, view), view.sort, now)
    # Checkbox counts: how many runs each choice would show with the other filters kept.
    state_counts = Counter(r.state for r in filter_runs(runs, replace(view, states=())))
    reason_counts = Counter(r.reason_group for r in filter_runs(runs, replace(view, reasons=())))

    messages = []
    if error:
        messages.append(f'<p class="alert error">{esc(error)}</p>')
    elif not (view.org and view.project):
        messages.append('<p class="alert">Enter an organization and a project, then press Apply.</p>')
    if snapshot and snapshot.truncated:
        messages.append(f'<p class="alert">Only the newest {len(runs)} runs were loaded. '
                        "Choose a shorter time window to see all of them.</p>")
    if snapshot and snapshot.unresolved:
        messages.append(
            f'<p class="alert">{snapshot.unresolved} pull request run(s) could not be matched to the commit '
            f"that was pushed, so the Commit filter only matches their merge commit. "
            f"Reason: {esc(snapshot.unresolved_reason)}</p>"
        )
    if snapshot and snapshot.approvals_error:
        messages.append(
            '<p class="alert">Pending approvals could not be read, so runs waiting for approval are shown '
            f"as Running. Reason: {esc(snapshot.approvals_error)}</p>"
        )
    if snapshot and snapshot.stages_error:
        messages.append(
            '<p class="alert">The stage of some pending approvals could not be read from the run timeline. '
            f"Reason: {esc(snapshot.stages_error)}</p>"
        )
    waiting = state_counts.get("waitingForApproval", 0)
    if waiting and view.states != ("waitingForApproval",):
        only_waiting = _here("Show only those", view.to_query(states=("waitingForApproval",)))
        messages.append(
            f'<p class="alert waiting"><span class="dot"></span><b>{waiting}</b> '
            f"run{'s are' if waiting > 1 else ' is'} waiting for approval. {only_waiting}</p>"
        )

    data_age = ""
    if snapshot:
        loaded = datetime.fromtimestamp(snapshot.loaded_at, timezone.utc)
        data_age = f"Loaded {esc(format_ago(loaded, now))} · auth: {'PAT' if auth_mode == 'pat' else 'az login'}"

    summary = ""
    if snapshot:
        summary = (f"Showing <b>{len(shown)}</b> of {len(runs)} runs queued in "
                   f"{esc(WINDOW_LABELS[view.window].lower())}.")

    if shown:
        rows = "\n".join(_row(r, view, now) for r in shown)
    elif runs:
        rows = f'<tr><td class="empty" colspan="{len(COLUMNS)}">No runs match these filters.</td></tr>'
    else:
        rows = ""

    window_options = "".join(
        f'<option value="{k}"{" selected" if k == view.window else ""}>{esc(label)}</option>'
        for k, label in WINDOW_LABELS.items()
    )
    branches = [r.branch for r in runs if not r.pr_number]
    branches += [b for r in runs for b in (r.pr_source_branch, r.pr_target_branch)]
    datalists = "\n".join(
        (
            _datalist("dl-project", projects),
            _datalist("dl-pipeline", (r.pipeline for r in runs)),
            _datalist("dl-by", (r.requested_for for r in runs)),
            _datalist("dl-branch", branches),
        )
    )

    return _TEMPLATE.substitute(
        refresh_meta=f'<meta http-equiv="refresh" content="{AUTO_REFRESH_SECONDS}">' if view.auto else "",
        title=esc(f"Pipeline runs · {view.project}" if view.project else "Pipeline runs"),
        org=esc(view.org),
        project=esc(view.project),
        window_options=window_options,
        auto_checked=" checked" if view.auto else "",
        data_age=data_age,
        refresh_href=esc(f"/refresh?{view.to_query()}"),
        f_pipeline=esc(view.pipeline),
        f_run=esc(view.run),
        f_by=esc(view.requested_by),
        f_commit=esc(view.commit),
        f_branch=esc(view.branch),
        clear_href=esc(f"?{view.filters_cleared().to_query()}"),
        state_checks=_checks("state", STATES, STATE_LABELS, view.states, state_counts),
        reason_checks=_checks("reason", REASONS, REASON_LABELS, view.reasons, reason_counts),
        sort_value=esc(format_sort(view.sort)),
        messages="\n".join(messages),
        summary=summary,
        head=_head(view),
        rows=rows,
        datalists=datalists,
    )
