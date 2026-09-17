"""The upload flow, against a server that behaves the way the app does."""

import http.server
import json
import threading

import pytest

from emi_analyzer.client import ApiError, Client


class Recorder:
    def __init__(self):
        self.requests = []
        self.already_uploaded = False
        self.fail = None  # (status, message)


@pytest.fixture
def app():
    state = Recorder()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _read(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.requests.append(("GET", self.path, b""))
            if state.fail:
                self._send(state.fail[0], {"error": state.fail[1]})
            elif self.path.startswith("/api/emi/boards/lookup"):
                self._send(200, {"found": False})
            else:
                self._send(200, {"ok": True})

        def do_POST(self):
            body = self._read()
            state.requests.append(("POST", self.path, body))
            if self.path.endswith("/uploads"):
                self._send(200, {
                    "key": "inputs/abc",
                    "content_type": "application/zip",
                    "already_uploaded": state.already_uploaded,
                    "upload_url": f"http://127.0.0.1:{self.server.server_port}/blob/inputs/abc?op=put",
                })
            elif self.path.endswith("/boards"):
                self._send(200, {"board": {"id": "b1"}, "run": {"id": "r1"}})
            else:
                self._send(200, {"id": "p1"})

        def do_PUT(self):
            state.requests.append(("PUT", self.path, self._read()))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.client = Client(f"http://127.0.0.1:{server.server_port}")
    try:
        yield state
    finally:
        server.shutdown()


def test_the_bytes_go_straight_to_storage_and_the_app_hears_about_them_after(app):
    got = app.client.upload_board("p1", "demo.zip", b"PK-the-board", "a" * 64)

    assert got == {"board": {"id": "b1"}, "run": {"id": "r1"}}
    methods = [(m, p.split("?")[0]) for m, p, _ in app.requests]
    assert methods == [
        ("POST", "/api/emi/projects/p1/uploads"),
        ("PUT", "/blob/inputs/abc"),
        ("POST", "/api/emi/projects/p1/boards"),
    ]
    assert app.requests[1][2] == b"PK-the-board"
    assert json.loads(app.requests[0][2])["sha256"] == "a" * 64


def test_a_board_the_app_already_holds_sends_no_bytes(app):
    """Opening a board again unchanged is the common case; it must cost nothing."""
    app.already_uploaded = True
    app.client.upload_board("p1", "demo.zip", b"PK-the-board", "a" * 64)
    assert [m for m, _, _ in app.requests] == ["POST", "POST"]


def test_an_error_from_the_app_carries_its_own_words(app):
    app.fail = (413, "the board is larger than the app accepts")
    with pytest.raises(ApiError) as e:
        app.client.lookup_board("a" * 64)
    assert e.value.status == 413
    assert "larger than" in str(e.value)


def test_an_app_that_is_not_there_is_an_error_not_a_hang():
    client = Client("http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(ApiError) as e:
        client.status()
    assert e.value.status == 0
    assert "could not be reached" in str(e.value)


def test_a_board_too_large_to_send_is_refused_before_anything_moves(app):
    from emi_analyzer import client as clientlib

    with pytest.raises(ApiError):
        app.client.upload_board("p1", "demo.zip", b"x" * (clientlib.MAX_UPLOAD_BYTES + 1), "a" * 64)
    assert app.requests == []
