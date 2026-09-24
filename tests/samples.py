"""Payloads shaped like real Azure DevOps API responses, with placeholder
names and ids (Fabrikam is a fictitious company)."""

import copy

REPO_ID = "11111111-2222-4333-8444-555555555555"
CI_SHA = "c0ffee5a1b2c3d4e5f60718293a4b5c6d7e8f901"
PR_MERGE_SHA = "5ca1ab1e0123456789abcdef0123456789abcdef"
PR_TARGET_SHA = "7a26e70123456789abcdef0123456789abcdef01"
PR_SOURCE_SHA = "b0a710ad0123456789abcdef0123456789abcdef"

_CI = {
    "id": 12363,
    "buildNumber": "1.37.1-1",
    "status": "completed",
    "result": "succeeded",
    "reason": "individualCI",
    "sourceBranch": "refs/heads/main",
    "sourceVersion": CI_SHA,
    "queueTime": "2026-09-24T07:47:42.5392536Z",
    "startTime": "2026-09-24T07:48:00Z",
    "finishTime": "2026-09-24T07:51:30Z",
    "definition": {"id": 305, "name": "orders-api-ci", "path": "\\orders-api"},
    "triggerInfo": {
        "ci.sourceBranch": "refs/heads/main",
        "ci.sourceSha": CI_SHA,
        "ci.message": "Merged PR 10205: Merge develop into main",
        "ci.triggerRepository": REPO_ID,
    },
    "requestedFor": {"displayName": "Zoë Example", "uniqueName": "zoe@example.com"},
    "requestedBy": {"displayName": "Microsoft.VisualStudio.Services.TFS"},
    "repository": {
        "id": REPO_ID,
        "type": "TfsGit",
        "name": "FabrikamPlatform",
        "url": "https://dev.azure.com/fabrikam/Fabrikam%20Fiber/_git/FabrikamPlatform",
    },
    "project": {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "name": "Fabrikam Fiber"},
    "_links": {
        "web": {
            "href": "https://dev.azure.com/fabrikam/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/_build/results?buildId=12363"
        }
    },
    "tags": [],
}

_PR = {
    **_CI,
    "id": 12349,
    "buildNumber": "20260923.4",
    "status": "inProgress",
    "result": None,
    "reason": "pullRequest",
    "sourceBranch": "refs/pull/10206/merge",
    "sourceVersion": PR_MERGE_SHA,
    "queueTime": "2026-09-23T14:02:00Z",
    "startTime": "2026-09-23T14:02:10Z",
    "finishTime": None,
    "definition": {"id": 196, "name": "storefront-web-ci", "path": "\\storefront-web"},
    "triggerInfo": {
        "pr.number": "10206",
        "pr.isFork": "False",
        "pr.triggerRepository": REPO_ID,
        "pr.triggerRepository.Type": "TfsGit",
    },
    "requestedFor": {"displayName": "Alex Example", "uniqueName": "alex@example.com"},
    "requestedBy": {"displayName": "Alex Example"},
    "_links": {
        "web": {
            "href": "https://dev.azure.com/fabrikam/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/_build/results?buildId=12349"
        }
    },
}

PR_MERGE_COMMIT = {
    "commitId": PR_MERGE_SHA,
    "parents": [PR_TARGET_SHA, PR_SOURCE_SHA],
    "comment": "Merge pull request 10206 from feature/order-export into develop",
}


APPROVAL_ID = "0a0a0a0a-0000-4000-8000-000000000001"
STAGE_RECORD_ID = "5a5a5a5a-0000-4000-8000-000000000002"

# GET _apis/pipelines/approvals?state=pending&$expand=steps -> value[0]
APPROVAL = {
    "id": APPROVAL_ID,
    "status": "pending",
    "createdOn": "2026-09-24T07:54:41.507Z",
    "executionOrder": "anyOrder",
    "minRequiredApprovers": 1,
    "blockedApprovers": [],
    "steps": [
        {"assignedApprover": {"displayName": "Zoë Example"}, "status": "pending", "order": 1},
        {"assignedApprover": {"displayName": "Alex Example"}, "status": "pending", "order": 1},
        {"assignedApprover": {"displayName": "Sam Example"}, "status": "rejected", "order": 1},
    ],
    "pipeline": {"owner": {"id": 12363, "name": "20260924.2"}, "id": "305", "name": "orders-api-ci"},
}

# GET _apis/build/builds/12363/timeline -> records (trimmed)
TIMELINE = [
    {"id": "5a5a5a5a-0000-4000-8000-000000000001", "type": "Stage", "name": "Build",
     "state": "completed", "parentId": None},
    {"id": STAGE_RECORD_ID, "type": "Stage", "name": "Deploy to Staging", "state": "pending",
     "parentId": None},
    {"id": "c4c4c4c4-0000-4000-8000-000000000002", "type": "Checkpoint", "name": "Checkpoint", "state": "inProgress",
     "parentId": STAGE_RECORD_ID},
    {"id": APPROVAL_ID, "type": "Checkpoint.Approval", "name": "Checkpoint.Approval", "state": "inProgress",
     "parentId": "c4c4c4c4-0000-4000-8000-000000000002"},
    {"id": "5a5a5a5a-0000-4000-8000-000000000003", "type": "Stage", "name": "Deploy to Demo",
     "state": "completed", "parentId": None},
    {"id": "c4c4c4c4-0000-4000-8000-000000000003", "type": "Checkpoint", "name": "Checkpoint", "state": "completed",
     "parentId": "5a5a5a5a-0000-4000-8000-000000000003"},
    {"id": "0a0a0a0a-0000-4000-8000-000000000003", "type": "Checkpoint.Approval", "name": "Checkpoint.Approval",
     "state": "completed", "parentId": "c4c4c4c4-0000-4000-8000-000000000003"},
]


def waiting_build(**overrides) -> dict:
    """Build 12363 while it is held at the 'Deploy to Staging' approval."""
    return ci_build(status="inProgress", result=None, finishTime=None, **overrides)


def ci_build(**overrides) -> dict:
    return {**copy.deepcopy(_CI), **overrides}


def pr_build(**overrides) -> dict:
    return {**copy.deepcopy(_PR), **overrides}
