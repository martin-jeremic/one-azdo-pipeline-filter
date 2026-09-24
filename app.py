"""one-azdo-pipeline-filter: a local page listing Azure DevOps pipeline runs,
with filters the portal lacks: by commit, and runs waiting for approval.

    python app.py [--org ORG] [--project PROJECT] [--port 8765] [--open]

Organization and project default to `az devops configure --defaults`.
Credentials: AZDO_PAT (or AZURE_DEVOPS_EXT_PAT) if set, otherwise `az login`.
"""

import argparse
import sys
import traceback
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ado import AdoClient, AdoError, auth_from_env, org_from_url, read_az_devops_defaults
from page import AUTO_REFRESH_SECONDS, render_page
from runs import View, utcnow
from store import RunStore

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
# Lookups of facts that never change (PR commits, approval stages).
CACHE_DIR = HERE / ".cache"
DEFAULT_PORT = 8765
DEFAULT_MAX_AGE = 5 * 60

STATIC_TYPES = {".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SECURITY_HEADERS = {
    # The page has no script at all, so none is allowed to run.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class App:
    def __init__(self, store: RunStore, org: str, project: str, auth_mode: str, max_age: float = DEFAULT_MAX_AGE):
        self.store = store
        self.org = org
        self.project = project
        self.auth_mode = auth_mode
        self.max_age = max_age

    def view(self, query: str) -> View:
        v = View.from_query(query)
        return replace(v, org=org_from_url(v.org) or self.org, project=v.project or self.project)


class RunsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app: App):
        super().__init__(address, Handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "one-azdo-pipeline-filter"
    sys_version = ""

    def do_GET(self):
        # This server acts with your Azure DevOps identity. Accepting only its
        # own host names stops a web page that re-points (DNS-rebinds) its
        # hostname to 127.0.0.1 from reading your data through it.
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send(403, "text/plain; charset=utf-8", b"Forbidden host")
            return
        url = urlsplit(self.path)
        if url.path == "/":
            self._page(url.query)
        elif url.path == "/refresh":
            self._refresh(url.query)
        elif url.path.startswith("/static/"):
            self._static(url.path[len("/static/"):])
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _page(self, query: str):
        app: App = self.server.app
        view = app.view(query)
        snapshot, error = None, ""
        try:
            if view.org and view.project:
                max_age = AUTO_REFRESH_SECONDS if view.auto else app.max_age
                snapshot = app.store.runs(view.org, view.project, view.window, max_age)
        except AdoError as e:
            error = str(e)
        except Exception as e:
            # Shown on the page rather than as a dropped connection.
            traceback.print_exc()
            error = f"Unexpected error: {e!r}"
        projects = app.store.projects(view.org) if view.org else []
        body = render_page(view, snapshot, error, projects, app.auth_mode, utcnow())
        self._send(200, "text/html; charset=utf-8", body.encode("utf-8"))

    def _refresh(self, query: str):
        app: App = self.server.app
        view = app.view(query)
        if view.org and view.project:
            app.store.invalidate(view.org, view.project, view.window)
        self.send_response(303)
        self.send_header("Location", f"/?{view.to_query()}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name: str):
        target = (STATIC_DIR / unquote(name)).resolve()
        content_type = STATIC_TYPES.get(target.suffix)
        if STATIC_DIR not in target.parents or not content_type or not target.is_file():
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        self._send(200, content_type, target.read_bytes(), cache="no-cache")

    def _send(self, status: int, content_type: str, body: bytes, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Browse and filter Azure DevOps pipeline runs.")
    parser.add_argument("--org", help="organization name or URL (default: az devops configure default)")
    parser.add_argument("--project", help="project name (default: az devops configure default)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_AGE,
        help=f"seconds before loaded runs are fetched again (default {DEFAULT_MAX_AGE})",
    )
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args(argv)

    default_org, default_project = read_az_devops_defaults()
    org = org_from_url(args.org or "") or default_org
    project = args.project or default_project
    auth = auth_from_env()
    app = App(RunStore(AdoClient(auth), CACHE_DIR), org, project, auth.mode, args.max_age)
    try:
        server = RunsServer(("127.0.0.1", args.port), app)
    except OSError as e:
        print(f"one-azdo-pipeline-filter: cannot listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"one-azdo-pipeline-filter on {url}")
    print(f"  organization: {org or '(not set)'}   project: {project or '(not set)'}   auth: {auth.mode}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
