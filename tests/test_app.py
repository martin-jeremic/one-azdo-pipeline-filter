import http.client
import threading
import unittest
from urllib.parse import quote

from ado import AdoError
from app import App, RunsServer
from store import RunStore
from tests.fakes import FakeClient
from tests.samples import APPROVAL, PR_SOURCE_SHA, ci_build, pr_build, waiting_build


class ServerTest(unittest.TestCase):
    def start(self, client=None, org="fabrikam", project="Fabrikam Fiber"):
        self.client = client or FakeClient()
        server = RunsServer(("127.0.0.1", 0), App(RunStore(self.client), org, project, "az"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def request(self, path, method="GET", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        resp = conn.getresponse()
        return resp, resp.read().decode("utf-8", "replace")

    def test_page_lists_runs_without_any_script(self):
        self.start()
        resp, body = self.request("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("orders-api-ci", body)
        self.assertIn("storefront-web-ci", body)
        self.assertIn("Showing <b>2</b> of 2 runs", body)
        self.assertNotIn("<script", body.lower())
        self.assertTrue(resp.getheader("Content-Security-Policy").startswith("default-src 'none'"))
        self.assertIn('value="fabrikam"', body)

    def test_commit_filter_finds_the_pr_run_by_its_pushed_commit(self):
        self.start()
        _, body = self.request(f"/?commit={PR_SOURCE_SHA[:7]}")
        self.assertIn("Showing <b>1</b> of 2 runs", body)
        self.assertIn(">storefront-web-ci<", body)
        self.assertNotIn(">orders-api-ci<", body)

    def test_localhost_is_accepted_and_other_hosts_are_refused(self):
        self.start()
        self.assertEqual(self.request("/", host=f"localhost:{self.port}")[0].status, 200)
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1"):
            self.assertEqual(self.request("/", host=host)[0].status, 403, host)
        self.assertEqual(self.client.build_calls, 1)

    def test_only_get_is_served(self):
        self.start()
        self.assertEqual(self.request("/", method="POST")[0].status, 501)

    def test_values_from_azure_devops_are_escaped(self):
        self.start(FakeClient(builds=[ci_build(definition={"id": 1, "name": "<script>alert(1)</script>"})]))
        _, body = self.request("/?pipeline=%3Cb%3E")
        self.assertNotIn("<script>alert(1)", body)
        self.assertNotIn('value="<b>"', body)
        self.assertIn('value="&lt;b&gt;"', body)

    def test_errors_are_shown_on_the_page(self):
        self.start(FakeClient(error=AdoError("Azure DevOps rejected the az login token (HTTP 401)", 401)))
        resp, body = self.request("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("rejected the az login token", body)

    def test_missing_org_asks_for_it(self):
        self.start(org="", project="")
        _, body = self.request("/")
        self.assertIn("Enter an organization and a project", body)
        self.assertEqual(self.client.build_calls, 0)

    def test_refresh_reloads_and_redirects_back(self):
        self.start()
        self.request("/?window=7d")
        resp, _ = self.request("/refresh?window=7d&commit=abc")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/?org=fabrikam&project=Fabrikam+Fiber&window=7d&commit=abc")
        self.request("/?window=7d")
        self.assertEqual(self.client.build_calls, 2)

    def test_sort_links_and_secondary_sort(self):
        self.start()
        _, body = self.request("/?sort=pipeline:desc")
        self.assertIn(quote("pipeline:asc"), body)  # clicking Pipeline again reverses it
        self.assertIn(quote("pipeline:desc,queued:desc"), body)  # "+" on Queued
        # The default (newest queued first) would list orders-api-ci first.
        first, second = body.index(">storefront-web-ci<"), body.index(">orders-api-ci<")
        self.assertLess(first, second)

    def test_static_files_and_traversal(self):
        self.start()
        resp, body = self.request("/static/style.css")
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/css; charset=utf-8"))
        for path in ("/static/../app.py", "/static/..%2fapp.py", "/static/%2e%2e/app.py", "/static/", "/app.py"):
            self.assertEqual(self.request(path)[0].status, 404, path)

    def test_runs_waiting_for_approval_are_called_out(self):
        self.start(FakeClient(builds=[waiting_build(), pr_build()], approvals=[APPROVAL]))
        _, body = self.request("/")
        self.assertIn('<tr class="s-waitingForApproval">', body)
        self.assertIn("Waiting for approval", body)
        self.assertIn("Deploy to Staging · waiting", body)
        self.assertIn("Zoë Example, Alex Example (1 approval needed)", body)
        self.assertIn("<b>1</b> run is waiting for approval.", body)
        self.assertIn("state=waitingForApproval", body)

        _, body = self.request("/?state=waitingForApproval")
        self.assertIn("Showing <b>1</b> of 2 runs", body)
        self.assertNotIn("run is waiting for approval.", body)  # the banner's link is already applied

    def test_unreadable_approvals_are_explained(self):
        self.start(FakeClient(builds=[waiting_build()], approvals_error=AdoError("HTTP 403: no access", 403)))
        _, body = self.request("/")
        self.assertIn("Pending approvals could not be read", body)
        self.assertIn('<tr class="s-inProgress">', body)

    def test_pr_rows_show_the_pushed_commit(self):
        self.start(FakeClient(builds=[pr_build()]))
        _, body = self.request("/")
        self.assertIn(f"/commit/{PR_SOURCE_SHA}", body)
        self.assertIn('class="badge"', body)
        self.assertIn("feature/order-export → develop", body)


if __name__ == "__main__":
    unittest.main()
