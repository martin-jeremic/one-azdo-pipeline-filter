"""Keeps the loaded runs of each (org, project, window) and adds what the
builds API does not return: the commit pushed to a pull request, and the
approvals a run is waiting for."""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ado import AdoError
from runs import (
    WINDOWS,
    Approval,
    Run,
    apply_approvals,
    apply_merge_commit,
    approval_stages,
    needs_merge_commit,
    normalize_build,
    parse_approval,
    utcnow,
)

LOOKUP_WORKERS = 6


@dataclass
class Snapshot:
    runs: list[Run]
    loaded_at: float
    truncated: bool = False
    # PR builds whose pushed commit could not be looked up, and why.
    unresolved: int = 0
    unresolved_reason: str = ""
    # Why pending approvals, or the stages they gate, could not be read.
    approvals_error: str = ""
    stages_error: str = ""


class JsonCache:
    """A string-keyed map kept in a JSON file, for facts that never change once
    they exist, such as a commit's parents or the stage an approval belongs to."""

    def __init__(self, path: Path | None):
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict = {}
        if path:
            try:
                self._entries = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._entries = {}

    def get(self, key: str):
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, value) -> None:
        with self._lock:
            self._entries[key] = value

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            text = json.dumps(self._entries, separators=(",", ":"))
        tmp = self._path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            # The lookups still apply to this session; they are only not kept for the next one.
            print(f"one-azdo-pipeline-filter: could not write {self._path}: {e}", file=sys.stderr)


class RunStore:
    def __init__(self, client, cache_dir: Path | None = None, clock=time.time):
        self._client = client
        self._clock = clock
        # (repo id:merge sha) -> [second parent, first line of the commit message]
        self._merge = JsonCache(cache_dir / "merge-commits.json" if cache_dir else None)
        # approval id -> name of the stage it gates
        self._stages = JsonCache(cache_dir / "approval-stages.json" if cache_dir else None)
        self._lock = threading.Lock()
        self._snapshots: dict[tuple, Snapshot] = {}
        self._key_locks: dict[tuple, threading.Lock] = {}
        self._projects: dict[str, list[str]] = {}

    @staticmethod
    def _key(org: str, project: str, window: str) -> tuple:
        return org.casefold(), project.casefold(), window

    def _key_lock(self, key) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def runs(self, org: str, project: str, window: str, max_age: float) -> Snapshot:
        """Cached runs, reloaded when older than max_age seconds. Concurrent
        requests for the same key wait for one load instead of each loading."""
        key = self._key(org, project, window)
        with self._key_lock(key):
            snapshot = self._snapshots.get(key)
            if snapshot is None or self._clock() - snapshot.loaded_at >= max_age:
                snapshot = self._load(org, project, window)
                self._snapshots[key] = snapshot
            return snapshot

    def invalidate(self, org: str, project: str, window: str) -> None:
        key = self._key(org, project, window)
        with self._key_lock(key):
            self._snapshots.pop(key, None)

    def projects(self, org: str) -> list[str]:
        """Project names for the project field's suggestions. Empty when they
        can't be listed; the runs request then reports the actual error."""
        key = org.casefold()
        if key not in self._projects:
            try:
                self._projects[key] = self._client.list_projects(org)
            except AdoError:
                self._projects[key] = []
        return self._projects[key]

    def _load(self, org: str, project: str, window: str) -> Snapshot:
        loaded_at = self._clock()
        span = WINDOWS[window]
        min_time = utcnow() - timedelta(seconds=span) if span else None
        builds, truncated = self._client.list_builds(org, project, min_time)
        runs = [normalize_build(b) for b in builds]
        snapshot = Snapshot(runs, loaded_at, truncated)
        snapshot.unresolved, snapshot.unresolved_reason = self._resolve_merge_commits(org, runs)
        snapshot.approvals_error, snapshot.stages_error = self._apply_approvals(org, project, runs)
        return snapshot

    def _resolve_merge_commits(self, org: str, runs: list[Run]) -> tuple[int, str]:
        pending: dict[str, list[Run]] = {}
        for r in runs:
            if needs_merge_commit(r):
                pending.setdefault(f"{r.repo_id}:{r.sha}", []).append(r)
        missing = [key for key in pending if self._merge.get(key) is None]
        failed, reason = 0, ""
        if missing:
            with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
                futures = {
                    pool.submit(self._client.get_commit, org, *key.split(":", 1)): key for key in missing
                }
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        commit = future.result()
                    except AdoError as e:
                        failed += len(pending[key])
                        reason = str(e)
                        continue
                    parents = commit.get("parents") or []
                    first_line = (commit.get("comment") or "").split("\n", 1)[0]
                    self._merge.put(key, [parents[1] if len(parents) > 1 else "", first_line])
            self._merge.save()
        for key, rows in pending.items():
            info = self._merge.get(key)
            if info:
                for r in rows:
                    apply_merge_commit(r, *info)
        return failed, reason

    def _apply_approvals(self, org: str, project: str, runs: list[Run]) -> tuple[str, str]:
        """Marks runs held at an approval. Returns (approvals error, stages error)."""
        try:
            raw = self._client.list_pending_approvals(org, project)
        except AdoError as e:
            return str(e), ""
        by_id = {r.id: r for r in runs}
        pending: dict[int, list[Approval]] = {}
        for item in raw:
            parsed = parse_approval(item)
            if parsed and parsed[0] in by_id:
                pending.setdefault(parsed[0], []).append(parsed[1])

        # The stage an approval gates is only in the run's timeline.
        stages_error = ""
        missing = [b for b, approvals in pending.items() if any(self._stages.get(a.id) is None for a in approvals)]
        if missing:
            with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
                futures = {pool.submit(self._client.get_timeline, org, project, b): b for b in missing}
                for future in as_completed(futures):
                    try:
                        records = future.result()
                    except AdoError as e:
                        stages_error = str(e)
                        continue
                    for approval_id, stage in approval_stages(records).items():
                        self._stages.put(approval_id, stage)
            self._stages.save()

        for build_id, approvals in pending.items():
            for a in approvals:
                a.stage = self._stages.get(a.id) or ""
            apply_approvals(by_id[build_id], approvals)
        return "", stages_error
