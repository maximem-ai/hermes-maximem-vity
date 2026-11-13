"""Browser sign-in (hermes_maximem_vity.login) against a fake sign-in server and router on localhost.

The "browser" is a function that reads the authorize URL the way the real proxy would and then calls
the plugin's loopback callback. The fake token endpoint enforces PKCE S256, so a test passes only if
the verifier the plugin sends really hashes to the challenge it put in the URL.
"""
import base64
import hashlib
import json
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_maximem_vity import installer, login

KEY = "mx_" + "a" * 40


class Fake:
    """One server playing both the proxy (/oauth/token) and the router (/v1/setup/keys)."""

    def __init__(self):
        self.challenge = None
        self.token_status = 200
        self.token_body = None
        self.keys_status = 200
        self.keys_body = {"account_email": "me@example.com", "keys": [{"app": "hermes", "key_id": "k1", "full_key": KEY}]}
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.calls.append(("POST", self.path, raw, self.headers.get("Authorization")))
                if self.path == "/oauth/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                    if digest != fake.challenge or form["client_id"] != "maximem-hermes-plugin" or form["code"] != "the-code":
                        return self._reply(400, {"error": "invalid_grant"})
                    return self._reply(fake.token_status, fake.token_body or {"access_token": "setup-token"})
                if self.path == "/v1/setup/keys":
                    return self._reply(fake.keys_status, fake.keys_body)
                self._reply(404, None)

            def do_DELETE(self):  # noqa: N802
                fake.calls.append(("DELETE", self.path, b"", self.headers.get("Authorization")))
                self._reply(204, None)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def browser(fake, *, before=None, query=None):
    """Acts as the proxy + user: checks the authorize URL, then calls the loopback callback."""
    seen = {}

    def visit(q):
        cb = q["redirect_uri"]
        for path_and_query in before or []:
            seen.setdefault("before", []).append(get(cb.rsplit("/callback", 1)[0] + path_and_query))
        seen["callback"] = get(f"{cb}?{urllib.parse.urlencode(query or {'code': 'the-code', 'state': q['state']})}")

    return seen, in_browser(fake, seen, visit)


def in_browser(fake, seen, visit):
    """Like a real browser, the visit runs while the plugin is listening, not inside open_url."""
    def open_url(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)
        fake.challenge = q["code_challenge"]
        t = threading.Thread(target=visit, args=(q,), daemon=True)
        t.start()
        seen["thread"] = t
    return open_url


def sign_in(fake, open_url, **kw):
    return login.browser_sign_in(auth_url=fake.url, resource="https://router.test/v1/setup", machine="Box",
                                 open_url=open_url, log=lambda _l: None, **kw)


def test_full_sign_in_sends_a_correct_pkce_s256_request(fake):
    seen, open_url = browser(fake)
    assert sign_in(fake, open_url) == "setup-token"
    assert seen["client_id"] == "maximem-hermes-plugin"
    assert seen["scope"] == "keys:create" and seen["resource"] == "https://router.test/v1/setup"
    assert seen["code_challenge_method"] == "S256" and seen["response_type"] == "code"
    assert seen["machine"] == "Box"
    assert len(seen["state"]) == 32
    seen["thread"].join(5)
    host = urllib.parse.urlsplit(seen["redirect_uri"])
    assert host.hostname == "127.0.0.1" and host.path == "/callback"
    assert seen["callback"][0] == 200 and "You are signed in" in seen["callback"][1]


def test_verifier_never_appears_in_the_authorize_url(fake):
    seen, open_url = browser(fake)
    sign_in(fake, open_url)
    form = dict(urllib.parse.parse_qsl(fake.calls[0][2].decode()))
    assert form["code_verifier"] not in json.dumps({k: v for k, v in seen.items() if k != "thread"})


def test_confirm_code_shape_and_it_is_printed_for_the_user(fake):
    lines = []
    seen, open_url = browser(fake)
    login.browser_sign_in(auth_url=fake.url, resource="r", open_url=open_url, log=lines.append)
    assert len(seen["confirm"]) == 9 and seen["confirm"][4] == "-"
    assert seen["confirm"].replace("-", "").isalnum() and seen["confirm"].upper() == seen["confirm"]
    assert seen["confirm"] in lines[0]


def test_wrong_state_and_other_paths_are_ignored_and_the_real_callback_still_works(fake):
    seen, open_url = browser(fake, before=[
        "/callback?code=evil&state=" + "0" * 32,
        "/callback?code=evil",
        "/",
        "/other?code=evil",
    ])
    assert sign_in(fake, open_url) == "setup-token"
    seen["thread"].join(5)
    statuses = [s for s, _ in seen["before"]]
    assert statuses == [400, 400, 404, 404]
    assert all("evil" not in body for _, body in seen["before"])


def test_nothing_from_the_request_is_reflected_into_the_page(fake):
    seen = {}

    def visit(q):
        seen["page"] = get(q["redirect_uri"] + "?" + urllib.parse.urlencode(
            {"state": q["state"], "code": "<script>x</script>", "error": "<img src=x>"}))[1]

    with pytest.raises(login.LoginError) as e:
        sign_in(fake, in_browser(fake, seen, visit))
    seen["thread"].join(5)
    assert e.value.reason == "cancelled"
    assert "<script>x" not in seen["page"] and "<img" not in seen["page"]


def test_browser_cancel_is_cancelled_and_no_token_request_is_made(fake):
    def visit(q):
        get(q["redirect_uri"] + "?" + urllib.parse.urlencode({"state": q["state"], "error": "access_denied"}))

    with pytest.raises(login.LoginError) as e:
        sign_in(fake, in_browser(fake, {}, visit))
    assert e.value.reason == "cancelled" and fake.calls == []


def test_timeout_when_the_browser_never_comes_back(fake):
    with pytest.raises(login.LoginError) as e:
        sign_in(fake, lambda url: None, timeout_s=0.3)
    assert e.value.reason == "timeout"


def test_listener_is_closed_after_sign_in(fake):
    seen, open_url = browser(fake)
    sign_in(fake, open_url)
    seen["thread"].join(5)
    with pytest.raises(Exception):
        urllib.request.urlopen(seen["redirect_uri"] + "?state=x", timeout=2)


@pytest.mark.parametrize("status,body,reason", [
    (404, {"x": 1}, "unavailable"),
    (400, {"error": "invalid_grant", "error_description": "SERVER-SECRET-TEXT"}, "refused"),
    (200, {"no_token": True}, "refused"),
])
def test_token_endpoint_failures_carry_fixed_text_only(fake, status, body, reason):
    fake.token_status, fake.token_body = status, body
    _, open_url = browser(fake)
    with pytest.raises(login.LoginError) as e:
        sign_in(fake, open_url)
    assert e.value.reason == reason and "SERVER-SECRET-TEXT" not in str(e.value)


def test_unreachable_sign_in_server():
    with pytest.raises(login.LoginError) as e:
        login._exchange("http://127.0.0.1:9", {"a": "b"})
    assert e.value.reason == "unreachable"


# ---------------------------------------------------------------- key creation

def create(fake):
    return login.create_hermes_key("setup-token", api_url=fake.url, machine="Box", machine_id_value="a" * 32)


def test_creates_one_hermes_key_with_the_setup_token(fake):
    assert create(fake) == ("me@example.com", KEY)
    method, path, raw, auth = fake.calls[0]
    assert (method, path, auth) == ("POST", "/v1/setup/keys", "Bearer setup-token")
    assert json.loads(raw) == {"apps": ["hermes"], "machine_id": "a" * 32, "machine_label": "Box"}


@pytest.mark.parametrize("body", [
    {"account_email": "me@example.com", "keys": [{"app": "cursor", "full_key": KEY}]},
    {"account_email": "me@example.com", "keys": [{"app": "hermes", "full_key": KEY}, {"app": "hermes", "full_key": KEY + "b"}]},
    {"account_email": "me‮@example.com", "keys": [{"app": "hermes", "full_key": KEY}]},
    {"account_email": "me\n@x.com", "keys": [{"app": "hermes", "full_key": KEY}]},
    {"keys": [{"app": "hermes", "full_key": KEY}]},
])
def test_an_unexpected_answer_is_refused_and_every_key_it_made_is_revoked(fake, body):
    fake.keys_body = body
    with pytest.raises(login.LoginError) as e:
        create(fake)
    assert e.value.reason == "invalid"
    revoked = [c[3] for c in fake.calls if c[0] == "DELETE"]
    assert sorted(revoked) == sorted(f"Bearer {k['full_key']}" for k in body["keys"])
    assert all(c[1] == "/v1/setup/keys/self" for c in fake.calls if c[0] == "DELETE")


@pytest.mark.parametrize("bad", ["not-a-key", "mx_short", "mx_" + "a" * 20 + "\n", 42])
def test_a_malformed_key_is_never_returned_or_sent_anywhere(fake, bad):
    fake.keys_body = {"account_email": "me@example.com", "keys": [{"app": "hermes", "full_key": bad}]}
    with pytest.raises(login.LoginError):
        create(fake)
    assert not [c for c in fake.calls if c[0] == "DELETE"]


@pytest.mark.parametrize("status,reason", [(404, "unavailable"), (401, "refused"), (429, "refused"), (500, "refused")])
def test_router_errors(fake, status, reason):
    fake.keys_status, fake.keys_body = status, {"error": {"message": "SERVER-SECRET-TEXT"}}
    with pytest.raises(login.LoginError) as e:
        create(fake)
    assert e.value.reason == reason and "SERVER-SECRET-TEXT" not in str(e.value)


# ---------------------------------------------------------------- machine identity

@pytest.mark.parametrize("host,label", [
    ("Udays-MacBook.local", "Udays-MacBook.local"),
    ("evil\u202ename<script>", "evil-name-script-"),
    ("", "this computer"),
    ("x" * 80, "x" * 40),
])
def test_machine_label_is_what_the_server_accepts(host, label):
    assert login.machine_label(host) == label


def test_machine_id_is_created_private_and_then_reused(tmp_path):
    first = login.machine_id(tmp_path)
    assert len(first) == 32 and int(first, 16) >= 0
    assert stat.S_IMODE((tmp_path / "machine-id").stat().st_mode) == 0o600
    assert login.machine_id(tmp_path) == first


def test_a_corrupt_machine_id_is_replaced(tmp_path):
    (tmp_path / "machine-id").write_text("../../etc/passwd\n")
    assert login.machine_id(tmp_path) != "../../etc/passwd"


# ---------------------------------------------------------------- installer wiring

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("MAXIMEM_API_KEY", raising=False)
    monkeypatch.delenv("VITY_API_KEY", raising=False)
    return tmp_path


def test_login_flag_saves_the_new_key_replacing_the_old_one(home, monkeypatch):
    (home / ".env").write_text("OTHER=1\nMAXIMEM_API_KEY=mx_old_key_value_123\n")
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: ("me@example.com", KEY))
    assert installer._ensure_api_key(None, login=True) is True
    assert (home / ".env").read_text() == f"OTHER=1\nMAXIMEM_API_KEY={KEY}\n"
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600


def test_failed_login_keeps_the_old_key_and_says_why(home, monkeypatch, capsys):
    (home / ".env").write_text("MAXIMEM_API_KEY=mx_old_key_value_123\n")

    def boom():
        raise login.LoginError("cancelled", "The sign-in was cancelled in the browser.")
    monkeypatch.setattr(login, "sign_in_for_hermes", boom)
    assert installer._ensure_api_key(None, login=True) is True  # the old key still works
    assert "mx_old_key_value_123" in (home / ".env").read_text()
    assert "cancelled in the browser" in capsys.readouterr().out


def test_failed_login_without_a_key_reports_no_key(home, monkeypatch):
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: (_ for _ in ()).throw(login.LoginError("timeout", "t")))
    assert installer._ensure_api_key(None, login=True) is False
    assert not (home / ".env").exists()


def _tty(monkeypatch, answer):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(installer.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)


def test_prompt_enter_signs_in_with_the_browser(home, monkeypatch):
    _tty(monkeypatch, "")
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: ("me@example.com", KEY))
    assert installer._ensure_api_key(None) is True
    assert f"MAXIMEM_API_KEY={KEY}" in (home / ".env").read_text()


def test_prompt_pasted_key_is_saved_without_signing_in(home, monkeypatch):
    _tty(monkeypatch, "mx_pasted_key_value_1")
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: pytest.fail("must not sign in"))
    assert installer._ensure_api_key(None) is True
    assert "MAXIMEM_API_KEY=mx_pasted_key_value_1" in (home / ".env").read_text()


def test_an_existing_key_is_kept_without_prompting_or_signing_in(home, monkeypatch):
    (home / ".env").write_text("MAXIMEM_API_KEY=mx_old_key_value_123\n")
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("must not prompt"))
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: pytest.fail("must not sign in"))
    assert installer._ensure_api_key(None) is True


def test_no_terminal_never_prompts_or_opens_a_browser(home, monkeypatch):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(login, "sign_in_for_hermes", lambda: pytest.fail("must not sign in"))
    assert installer._ensure_api_key(None) is False


def test_login_and_api_key_flags_cannot_be_combined():
    with pytest.raises(SystemExit):
        installer.main(["install", "--login", "--api-key", "mx_x"])
