"""Browser sign-in for ``hermes-maximem-vity install``: the user signs in on maximem.ai and the plugin
gets its own Maximem API key, so nobody has to copy one from the dashboard.

Same flow and safety properties as the npm CLI (clients/vity/lib/login.mjs in memoryinfra):

- OAuth authorization code + PKCE S256 with a loopback redirect to 127.0.0.1 only;
- the PKCE verifier never leaves this process;
- the listener answers ``GET /callback`` and nothing else, ignores any request whose ``state`` is
  wrong, reflects nothing from the request into its page, and stops on a valid callback or timeout;
- the user types a random confirmation code on the consent page, so a sign-in they did not start
  cannot be approved by accident;
- errors carry fixed text only: nothing from a server response is ever printed;
- the setup token lives only in memory; only the resulting key is written, by the caller.

The server decides what this client may do: client ``maximem-hermes-plugin`` can create a key for
the ``hermes`` app only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from . import __version__

CLIENT_ID = "maximem-hermes-plugin"
APP = "hermes"
SCOPE = "keys:create"
DEFAULT_AUTH_URL = "https://proxy-layers.maximem.ai"
DEFAULT_API_URL = "https://agenticrouter-prod.maximem.ai"
TIMEOUT_S = 10 * 60
REQUEST_S = 15
_CONFIRM_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_KEY = re.compile(r"mx_[A-Za-z0-9_-]{10,}")  # always fullmatch: re.match lets "$" pass a trailing newline
_EMAIL = re.compile(r"[^\s@]{1,64}@[^\s@]{1,255}")
_MACHINE_ID = re.compile(r"[a-f0-9]{32}")
# Cloudflare in front of the Maximem servers answers 403 to Python's default "Python-urllib" agent.
USER_AGENT = f"hermes-maximem-vity/{__version__}"
_PAGE = ('<!doctype html><meta charset="utf-8"><title>Maximem</title>'
         '<p style="font:16px system-ui;margin:3rem">{}</p>')


class LoginError(Exception):
    """reason: cancelled | timeout | unavailable | unreachable | refused | invalid"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _confirm_code() -> str:
    pick = lambda: "".join(secrets.choice(_CONFIRM_CHARS) for _ in range(4))  # noqa: E731
    return f"{pick()}-{pick()}"


def _printable_email(value: object) -> str | None:
    # The email goes to the terminal: no control, format (bidi) or separator characters.
    if not isinstance(value, str) or not _EMAIL.fullmatch(value) or not value.isprintable():
        return None
    return value


def machine_label(host: str | None = None) -> str:
    """Letters, digits, space and . _ - only, at most 40 characters (what the server accepts)."""
    raw = socket.gethostname() if host is None else host
    clean = re.sub(r"[^A-Za-z0-9 ._-]+", "-", str(raw)).strip()[:40].strip()
    return clean or "this computer"


def machine_id(directory: Path | None = None) -> str:
    """The id shared with ``npx @maximem/vity`` (~/.config/vity/machine-id), created if missing.
    Running sign-in again on this computer replaces this computer's Hermes key, not others'."""
    directory = directory or Path.home() / ".config" / "vity"
    path = directory / "machine-id"
    try:
        saved = path.read_text().strip()
    except OSError:
        saved = ""
    if _MACHINE_ID.fullmatch(saved):
        return saved
    new = secrets.token_hex(16)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(new + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return new


def open_browser(url: str) -> None:
    """VITY_NO_BROWSER=1 never launches one (the URL is printed anyway)."""
    if os.environ.get("VITY_NO_BROWSER"):
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass  # the URL is printed as well


def _listen(state: str) -> tuple[HTTPServer, dict]:
    """One loopback listener on 127.0.0.1. ``result`` gets {"code"} or {"cancelled"} from the one
    callback whose state matches; every other request is answered and ignored."""
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5  # a client that connects and sends nothing cannot hold the listener

        def _send(self, status: int, text: str | None = None) -> None:
            body = _PAGE.format(text).encode() if text else b""
            self.send_response(status)
            if text:
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/callback":
                return self._send(404)
            query = urllib.parse.parse_qs(url.query)
            got_state = (query.get("state") or [""])[0]
            if result or not hmac.compare_digest(got_state.encode(), state.encode()):
                return self._send(400, "This is not the sign-in you started.")
            code = (query.get("code") or [""])[0]
            ok = bool(code) and "error" not in query
            self._send(200, "You are signed in. You can close this tab and return to your terminal."
                       if ok else "Sign-in was not completed. You can close this tab.")
            result.update({"code": code} if ok else {"cancelled": True})

        def do_POST(self) -> None:  # noqa: N802
            self._send(404)

        def log_message(self, *args) -> None:  # the callback URL carries the code: never log it
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    return server, result


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_S) as res:  # noqa: S310 (fixed https bases)
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        raise LoginError("unreachable", "Could not reach the Maximem server.") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # report the 302 instead of following it
        return None


def _check_available(url: str) -> None:
    """Ask the sign-in server once, before opening the browser, whether it accepts this sign-in.
    An accepted request answers with a redirect (to sign-in or consent); a refused one (feature off,
    client not registered yet) answers with an error page. Without this check the browser shows
    that page and the terminal waits out the full timeout."""
    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=REQUEST_S).close()
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        raise LoginError("unreachable", "Could not reach the Maximem sign-in server.") from None
    if status not in (302, 303):
        raise LoginError("unavailable", "Browser sign-in is not available right now.")


def _exchange(auth_url: str, form: dict) -> str:
    status, body = _request(f"{auth_url}/oauth/token", data=urllib.parse.urlencode(form).encode(),
                            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    if status == 404:
        raise LoginError("unavailable", "Browser sign-in is not available right now.")
    if status != 200:
        raise LoginError("refused", f"The sign-in was refused (HTTP {status}).")
    try:
        token = json.loads(body).get("access_token")
    except Exception:
        token = None
    if not isinstance(token, str) or not token:
        raise LoginError("refused", "The sign-in server gave an unexpected answer.")
    return token


def browser_sign_in(*, auth_url: str | None = None, resource: str | None = None,
                    machine: str | None = None, open_url: Callable[[str], None] = open_browser,
                    log: Callable[[str], None] = print, timeout_s: float = TIMEOUT_S) -> str:
    """Signs in through the browser and returns a short-lived setup access token. Raises LoginError."""
    base = (auth_url or os.environ.get("VITY_AUTH_URL") or DEFAULT_AUTH_URL).rstrip("/")
    resource = resource or os.environ.get("VITY_SETUP_AUDIENCE") or f"{DEFAULT_API_URL}/v1/setup"
    verifier = _b64url(secrets.token_bytes(32))
    state = secrets.token_hex(16)
    confirm = _confirm_code()

    server, result = _listen(state)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        params = {
            "client_id": CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPE, "resource": resource,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256", "state": state, "confirm": confirm,
        }
        if machine:
            params["machine"] = machine
        url = f"{base}/oauth/authorize?{urllib.parse.urlencode(params)}"
        _check_available(url)
        log(f"Your confirmation code is {confirm}. Type it on the page to approve.")
        log(f"Opening your browser. If it does not open, go to:\n  {url}")
        open_url(url)

        deadline = time.monotonic() + timeout_s
        while not result:
            if time.monotonic() >= deadline:
                raise LoginError("timeout", "The sign-in timed out.")
            server.handle_request()
    finally:
        server.server_close()
    if result.get("cancelled"):
        raise LoginError("cancelled", "The sign-in was cancelled in the browser.")
    return _exchange(base, {"grant_type": "authorization_code", "code": result["code"],
                            "redirect_uri": redirect_uri, "client_id": CLIENT_ID,
                            "code_verifier": verifier})


def _revoke(api_url: str, key: str) -> None:
    """Best effort: a key the setup flow created may revoke itself."""
    try:
        _request(f"{api_url}/v1/setup/keys/self", headers={"Authorization": f"Bearer {key}"},
                 method="DELETE")
    except LoginError:
        pass


def create_hermes_key(access_token: str, *, api_url: str | None = None,
                      machine: str, machine_id_value: str) -> tuple[str, str]:
    """Trades the setup token for one Hermes key. Returns (account_email, key). Raises LoginError."""
    base = (api_url or os.environ.get("VITY_API_URL") or DEFAULT_API_URL).rstrip("/")
    status, body = _request(
        f"{base}/v1/setup/keys", method="POST",
        data=json.dumps({"apps": [APP], "machine_id": machine_id_value, "machine_label": machine}).encode(),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    if status == 404:
        raise LoginError("unavailable", "Browser sign-in is not available right now.")
    if status != 200:
        raise LoginError("refused", f"Maximem refused to create the key (HTTP {status}).")
    try:
        data = json.loads(body)
    except Exception:
        data = {}
    keys = [k for k in (data.get("keys") if isinstance(data, dict) else None) or []
            if isinstance(k, dict) and isinstance(k.get("full_key"), str) and _KEY.fullmatch(k["full_key"])]
    email = _printable_email(data.get("account_email") if isinstance(data, dict) else None)
    mine = [k["full_key"] for k in keys if k.get("app") == APP]
    if not email or len(mine) != 1 or len(keys) != 1:
        for k in keys:  # the server made keys we cannot use: do not leave them active
            _revoke(base, k["full_key"])
        raise LoginError("invalid", "Maximem gave an unexpected answer.")
    return email, mine[0]


def sign_in_for_hermes(log: Callable[[str], None] = print, **kw) -> tuple[str, str]:
    """Browser sign-in, then one Hermes key. Returns (account_email, key). Raises LoginError."""
    label = machine_label()
    token = browser_sign_in(machine=label, log=log, **kw)
    return create_hermes_key(token, machine=label, machine_id_value=machine_id())
