"""Azure DevOps access: default org/project from the az devops config, the
credential (a PAT or the signed-in az CLI identity), and the few read-only
REST calls the app makes."""

import base64
import configparser
import gzip
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ADO_ORIGIN = "https://dev.azure.com"
# Application ID of Azure DevOps: the resource an Entra token must be issued for.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
API_VERSION = "7.1"
# The approvals API has no released version yet.
APPROVALS_API_VERSION = "7.1-preview.1"
TOKEN_REFRESH_MARGIN = 5 * 60
BUILDS_PAGE_SIZE = 1000
MAX_RUNS = 10_000
APPROVALS_PAGE_SIZE = 500
MAX_APPROVALS = 5_000


class AdoError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def org_from_url(value: str) -> str:
    """'https://dev.azure.com/acme', 'https://acme.visualstudio.com' or 'acme' -> 'acme'."""
    v = (value or "").strip()
    if not v.lower().startswith(("http://", "https://")):
        return v
    u = urlsplit(v)
    host = (u.hostname or "").lower()
    if host.endswith(".visualstudio.com"):
        return host.split(".", 1)[0]
    return next((p for p in u.path.split("/") if p), "")


def read_az_devops_defaults(env=os.environ) -> tuple[str, str]:
    """The defaults written by `az devops configure --defaults organization=… project=…`."""
    config_dir = Path(env.get("AZURE_CONFIG_DIR") or Path.home() / ".azure")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_dir / "azuredevops" / "config", encoding="utf-8")
    except configparser.Error:
        return "", ""
    org = org_from_url(parser.get("defaults", "organization", fallback=""))
    project = parser.get("defaults", "project", fallback="").strip()
    return org, project


class PatAuth:
    mode = "pat"
    description = "the PAT in AZDO_PAT"

    def __init__(self, pat: str):
        self._header = "Basic " + base64.b64encode(f":{pat}".encode()).decode()

    def header(self) -> str:
        return self._header


def az_access_token() -> tuple[str, float]:
    """Returns (token, expiry as epoch seconds) for the signed-in az CLI identity."""
    az = shutil.which("az")
    if not az:
        raise AdoError("The az CLI is not on PATH. Install it and run \"az login\", or set AZDO_PAT.", 401)
    proc = subprocess.run(
        [az, "account", "get-access-token", "--resource", ADO_RESOURCE, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise AdoError(f"\"az account get-access-token\" failed: {detail}. Run \"az login\", or set AZDO_PAT.", 401)
    data = json.loads(proc.stdout)
    if data.get("expires_on"):
        expires = float(data["expires_on"])
    else:
        # Older az CLIs only give a naive local time: "2026-09-24 10:11:48.000000".
        expires = datetime.fromisoformat(data["expiresOn"]).timestamp()
    return data["accessToken"], expires


class AzCliAuth:
    mode = "az"
    description = "the az login token"

    def __init__(self, fetch_token=az_access_token, clock=time.time):
        self._fetch_token = fetch_token
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[str, float] | None = None

    def header(self) -> str:
        with self._lock:
            if self._cached is None or self._cached[1] - self._clock() <= TOKEN_REFRESH_MARGIN:
                token, expires = self._fetch_token()
                self._cached = (f"Bearer {token}", expires)
            return self._cached[0]


def auth_from_env(env=os.environ):
    # AZURE_DEVOPS_EXT_PAT is the variable the az devops extension itself reads.
    pat = env.get("AZDO_PAT") or env.get("AZURE_DEVOPS_EXT_PAT")
    return PatAuth(pat) if pat else AzCliAuth()


class _NoRedirect(HTTPRedirectHandler):
    # A redirect from ADO means "go and sign in"; surface it instead of following it.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", "replace")
    try:
        return json.loads(text).get("message") or ""
    except (ValueError, AttributeError):
        return "" if text.lstrip().startswith("<") else text[:300]


def _segment(value: str) -> str:
    return quote(value, safe="")


class AdoClient:
    def __init__(self, auth, opener=None):
        self.auth = auth
        self._opener = opener or build_opener(_NoRedirect())

    def get(self, path: str, api_version: str = API_VERSION, **params) -> tuple[dict, dict]:
        """GET https://dev.azure.com/<path>; returns (json body, response headers)."""
        query = urlencode({"api-version": api_version, **params}, safe="$")
        request = Request(
            f"{ADO_ORIGIN}/{path}?{query}",
            headers={
                "Authorization": self.auth.header(),
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with self._opener.open(request, timeout=120) as resp:
                status, headers, body = resp.status, resp.headers, resp.read()
        except HTTPError as e:
            status, headers, body = e.code, e.headers, e.read()
        except URLError as e:
            raise AdoError(f"Could not reach Azure DevOps: {e.reason}") from e
        if headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        is_json = "json" in (headers.get("Content-Type") or "")
        # Rejected credentials come back as 401, as a 203 with an HTML sign-in
        # page, or as a redirect to that page, depending on the credential type.
        if status == 401 or status == 203 or 300 <= status < 400 or (status < 300 and not is_json):
            detail = _error_detail(body)
            raise AdoError(
                f"Azure DevOps rejected {self.auth.description} (HTTP {status})" + (f": {detail}" if detail else ""),
                401,
            )
        if status >= 400:
            raise AdoError(_error_detail(body) or f"Azure DevOps returned HTTP {status}", status)
        return json.loads(body), headers

    def _paged(self, path: str, params: dict, limit: int, api_version: str = API_VERSION) -> tuple[list[dict], bool]:
        """Follows x-ms-continuationtoken. Returns (items, truncated at limit)."""
        items: dict = {}
        token = None
        while True:
            data, headers = self.get(
                path, api_version, **params, **({"continuationToken": token} if token else {})
            )
            page = data.get("value") or []
            # A continuation token can be a timestamp, so consecutive pages may overlap.
            for item in page:
                items.setdefault(item["id"], item)
            token = headers.get("x-ms-continuationtoken")
            if not token or not page:
                return list(items.values()), False
            if len(items) >= limit:
                return list(items.values()), True

    def list_builds(self, org: str, project: str, min_time: datetime | None) -> tuple[list[dict], bool]:
        """Builds queued at or after min_time, newest first. Returns (builds, truncated)."""
        params = {"queryOrder": "queueTimeDescending", "$top": BUILDS_PAGE_SIZE}
        if min_time:
            params["minTime"] = min_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._paged(f"{_segment(org)}/{_segment(project)}/_apis/build/builds", params, MAX_RUNS)

    def list_pending_approvals(self, org: str, project: str) -> list[dict]:
        """Every approval in the project that is still waiting, with its approvers."""
        params = {"state": "pending", "$expand": "steps", "top": APPROVALS_PAGE_SIZE}
        path = f"{_segment(org)}/{_segment(project)}/_apis/pipelines/approvals"
        approvals, _ = self._paged(path, params, MAX_APPROVALS, APPROVALS_API_VERSION)
        return approvals

    def get_timeline(self, org: str, project: str, build_id: int) -> list[dict]:
        data, _ = self.get(f"{_segment(org)}/{_segment(project)}/_apis/build/builds/{int(build_id)}/timeline")
        return data.get("records") or []

    def get_commit(self, org: str, repo_id: str, sha: str) -> dict:
        data, _ = self.get(f"{_segment(org)}/_apis/git/repositories/{_segment(repo_id)}/commits/{_segment(sha)}")
        return data

    def list_projects(self, org: str) -> list[str]:
        data, _ = self.get(f"{_segment(org)}/_apis/projects", **{"$top": 1000})
        return sorted((p["name"] for p in data.get("value") or []), key=str.casefold)
