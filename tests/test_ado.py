import base64
import gzip
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from ado import (
    AdoClient,
    AdoError,
    AzCliAuth,
    PatAuth,
    auth_from_env,
    org_from_url,
    read_az_devops_defaults,
)


def headers(**values) -> Message:
    msg = Message()
    for name, value in values.items():
        msg[name.replace("_", "-")] = value
    return msg


class FakeResponse:
    def __init__(self, status, body, hdrs):
        self.status, self._body, self.headers = status, body, hdrs

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Replays queued (status, body, headers) answers and records the requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        status, body, hdrs = self.answers.pop(0)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        if status >= 300 and status != 203:
            raise HTTPError(request.full_url, status, "error", hdrs, io.BytesIO(body))
        return FakeResponse(status, body, hdrs)


JSON = {"Content_Type": "application/json; charset=utf-8"}


class OrgAndDefaultsTest(unittest.TestCase):
    def test_org_from_url(self):
        self.assertEqual(org_from_url("https://dev.azure.com/fabrikam"), "fabrikam")
        self.assertEqual(org_from_url("https://dev.azure.com/fabrikam/"), "fabrikam")
        self.assertEqual(org_from_url("https://Fabrikam.VisualStudio.com/"), "fabrikam")
        self.assertEqual(org_from_url(" fabrikam "), "fabrikam")
        self.assertEqual(org_from_url(""), "")

    def test_reads_az_devops_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "azuredevops").mkdir()
            (Path(d) / "azuredevops" / "config").write_text(
                "[defaults]\norganization = https://dev.azure.com/fabrikam\nproject = Fabrikam Fiber\n\n"
                "[core]\nuse_git_aliases = no\n",
                encoding="utf-8",
            )
            self.assertEqual(read_az_devops_defaults({"AZURE_CONFIG_DIR": d}), ("fabrikam", "Fabrikam Fiber"))

    def test_missing_config_gives_no_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(read_az_devops_defaults({"AZURE_CONFIG_DIR": d}), ("", ""))


class AuthTest(unittest.TestCase):
    def test_pat_is_basic_auth_with_empty_user(self):
        header = PatAuth("secret").header()
        self.assertEqual(base64.b64decode(header.removeprefix("Basic ")), b":secret")

    def test_env_pat_wins_over_az(self):
        self.assertEqual(auth_from_env({"AZDO_PAT": "x"}).mode, "pat")
        self.assertEqual(auth_from_env({"AZURE_DEVOPS_EXT_PAT": "x"}).mode, "pat")
        self.assertEqual(auth_from_env({}).mode, "az")

    def test_az_token_is_cached_until_close_to_expiry(self):
        now = [1000.0]
        issued = []

        def fetch():
            issued.append(now[0])
            return f"token{len(issued)}", now[0] + 3600

        auth = AzCliAuth(fetch_token=fetch, clock=lambda: now[0])
        self.assertEqual(auth.header(), "Bearer token1")
        now[0] += 3000  # 10 minutes left
        self.assertEqual(auth.header(), "Bearer token1")
        now[0] += 400  # under 5 minutes left
        self.assertEqual(auth.header(), "Bearer token2")
        self.assertEqual(len(issued), 2)


class ClientTest(unittest.TestCase):
    def client(self, *answers):
        opener = FakeOpener(*answers)
        return AdoClient(PatAuth("secret"), opener=opener), opener

    def test_list_builds_follows_continuation_and_dedupes(self):
        client, opener = self.client(
            (200, {"value": [{"id": 3}, {"id": 2}]}, headers(**JSON, x_ms_continuationtoken="2026-09-20T00:00:00Z")),
            (200, {"value": [{"id": 2}, {"id": 1}]}, headers(**JSON)),
        )
        builds, truncated = client.list_builds("fabrikam", "Fabrikam Fiber", None)
        self.assertEqual([b["id"] for b in builds], [3, 2, 1])
        self.assertFalse(truncated)

        first, second = (urlsplit(r.full_url) for r in opener.requests)
        self.assertEqual(first.path, "/fabrikam/Fabrikam%20Fiber/_apis/build/builds")
        self.assertIn("$top=1000", first.query)
        self.assertEqual(parse_qs(first.query)["queryOrder"], ["queueTimeDescending"])
        self.assertEqual(parse_qs(second.query)["continuationToken"], ["2026-09-20T00:00:00Z"])
        self.assertTrue(opener.requests[0].get_header("Authorization").startswith("Basic "))

    def test_gzip_bodies_are_decoded(self):
        body = gzip.compress(json.dumps({"value": [{"id": 1}]}).encode())
        client, _ = self.client((200, body, headers(**JSON, Content_Encoding="gzip")))
        self.assertEqual(client.list_builds("o", "p", None)[0], [{"id": 1}])

    def test_sign_in_page_is_an_auth_error(self):
        for status, hdrs in (
            (203, headers(Content_Type="text/html")),
            (302, headers(Location="https://login.microsoftonline.com/")),
            (401, headers(**JSON)),
        ):
            client, _ = self.client((status, b"<html>sign in</html>", hdrs))
            with self.assertRaises(AdoError) as ctx:
                client.get("o/_apis/projects")
            self.assertEqual(ctx.exception.status, 401, status)
            self.assertIn("rejected the PAT", str(ctx.exception))

    def test_http_errors_carry_the_ado_message(self):
        client, _ = self.client((404, {"message": "TF200016: The project does not exist"}, headers(**JSON)))
        with self.assertRaises(AdoError) as ctx:
            client.list_builds("o", "nope", None)
        self.assertEqual((ctx.exception.status, str(ctx.exception)), (404, "TF200016: The project does not exist"))

    def test_pending_approvals_request(self):
        client, opener = self.client(
            (200, {"value": [{"id": "a1"}]}, headers(**JSON, x_ms_continuationtoken="t1")),
            (200, {"value": [{"id": "a2"}]}, headers(**JSON)),
        )
        self.assertEqual([a["id"] for a in client.list_pending_approvals("o", "Fabrikam Fiber")], ["a1", "a2"])
        first = urlsplit(opener.requests[0].full_url)
        self.assertEqual(first.path, "/o/Fabrikam%20Fiber/_apis/pipelines/approvals")
        query = parse_qs(first.query)
        self.assertEqual((query["state"], query["$expand"]), (["pending"], ["steps"]))
        self.assertEqual(query["api-version"], ["7.1-preview.1"])
        self.assertEqual(parse_qs(urlsplit(opener.requests[1].full_url).query)["continuationToken"], ["t1"])

    def test_timeline_path(self):
        client, opener = self.client((200, {"records": [{"id": "r"}]}, headers(**JSON)))
        self.assertEqual(client.get_timeline("o", "p", 12363), [{"id": "r"}])
        self.assertEqual(urlsplit(opener.requests[0].full_url).path, "/o/p/_apis/build/builds/12363/timeline")

    def test_commit_lookup_path(self):
        client, opener = self.client((200, {"parents": ["a", "b"]}, headers(**JSON)))
        self.assertEqual(client.get_commit("o", "repo-id", "abc")["parents"], ["a", "b"])
        self.assertEqual(urlsplit(opener.requests[0].full_url).path, "/o/_apis/git/repositories/repo-id/commits/abc")


if __name__ == "__main__":
    unittest.main()
