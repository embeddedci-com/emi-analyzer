"""Finding the app, and refusing to believe a file over a live server."""

import json

import pytest

from emi_analyzer import desktop
from emi_analyzer.desktop import AppUnavailable, Endpoint


@pytest.fixture
def endpoint_file(tmp_path, monkeypatch):
    path = tmp_path / "emi-analyzer" / "endpoint.json"
    monkeypatch.setattr(desktop, "endpoint_file", lambda: path)
    monkeypatch.delenv(desktop.URL_ENV, raising=False)
    return path


def write(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": "http://127.0.0.1:7465", "pid": 1, **fields}))


def test_no_file_means_no_app(endpoint_file):
    assert desktop.running() is None


def test_a_file_left_by_an_app_that_is_gone_is_not_believed(endpoint_file, monkeypatch):
    """The file survives a crash, a kill and a reboot. Only the server can settle it."""
    write(endpoint_file)
    monkeypatch.setattr(desktop, "probe", lambda url, timeout=0: None)
    assert desktop.running() is None


def test_an_app_that_answers_is_the_app(endpoint_file, monkeypatch):
    write(endpoint_file, version="0.2.0")
    monkeypatch.setattr(desktop, "probe", lambda url, timeout=0: Endpoint(url=url, version="0.2.0"))
    app = desktop.running()
    assert app.url == "http://127.0.0.1:7465"
    assert app.api == "http://127.0.0.1:7465/api"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:7465",
        "https://example.com",
        "file:///etc/passwd",
        "",
        7465,
        # All of these start with "http://127.0.0.1" and none of them is this computer.
        "http://127.0.0.1.example.com:7465",
        "http://127.0.0.1@example.com",
        "http://127.0.0.1:7465@example.com",
        "http://127.0.0.10:7465",
        "http://127.0.0.1:notaport",
    ],
)
def test_the_file_can_only_ever_point_at_this_computer(endpoint_file, monkeypatch, url):
    """This file decides where a board is sent, and a file is easier to write than a server."""
    write(endpoint_file, url=url)
    monkeypatch.setattr(desktop, "probe", lambda u, timeout=0: Endpoint(url=u))
    assert desktop.running() is None


def test_a_url_given_by_hand_skips_discovery(endpoint_file, monkeypatch):
    write(endpoint_file, url="http://127.0.0.1:7465")
    monkeypatch.setenv(desktop.URL_ENV, "http://127.0.0.1:9999")
    seen = []

    def probe(url, timeout=0):
        seen.append(url)
        return Endpoint(url=url)

    monkeypatch.setattr(desktop, "probe", probe)
    assert desktop.running().url == "http://127.0.0.1:9999"
    assert seen == ["http://127.0.0.1:9999"]


def test_a_url_given_by_hand_is_never_second_guessed_by_starting_the_app(endpoint_file, monkeypatch):
    monkeypatch.setenv(desktop.URL_ENV, "http://127.0.0.1:9999")
    monkeypatch.setattr(desktop, "probe", lambda url, timeout=0: None)
    monkeypatch.setattr(desktop, "start", lambda **kw: pytest.fail("started an app anyway"))
    with pytest.raises(AppUnavailable) as e:
        desktop.attach_or_start()
    assert "9999" in str(e.value)


def test_no_installed_app_says_what_to_do_about_it(monkeypatch):
    """The window turns this into a dialog with a "Get the app" button, so it stays short."""
    monkeypatch.setattr(desktop, "app_candidates", lambda: [])
    with pytest.raises(AppUnavailable) as e:
        desktop.start(timeout=0)
    message = str(e.value)
    assert "not installed" in message
    assert desktop.APP_ENV in message and desktop.URL_ENV in message


def test_only_one_copy_of_the_app_is_ever_started(monkeypatch):
    """Two ways of naming it can be two copies of it: two data folders, two worker containers."""
    spawned = []
    monkeypatch.setattr(desktop, "app_candidates", lambda: [["first"], ["second"], ["third"]])
    monkeypatch.setattr(desktop, "_spawn", lambda cmd: spawned.append(cmd[0]) is None)
    monkeypatch.setattr(desktop, "running", lambda: Endpoint(url="http://127.0.0.1:7465"))

    assert desktop.start(timeout=5).url == "http://127.0.0.1:7465"
    assert spawned == ["first"]


def test_the_next_way_of_starting_it_is_tried_when_the_first_does_nothing(monkeypatch):
    spawned = []
    monkeypatch.setattr(desktop, "PER_CANDIDATE_S", 0.2)
    monkeypatch.setattr(desktop, "app_candidates", lambda: [["first"], ["second"]])
    monkeypatch.setattr(desktop, "_spawn", lambda cmd: spawned.append(cmd[0]) is None)
    monkeypatch.setattr(desktop, "running", lambda: Endpoint(url="u") if len(spawned) == 2 else None)

    assert desktop.start(timeout=5).url == "u"
    assert spawned == ["first", "second"]


def test_an_app_that_does_not_come_up_is_reported_rather_than_waited_on(monkeypatch):
    monkeypatch.setattr(desktop, "app_candidates", lambda: [["true"]])
    monkeypatch.setattr(desktop, "_spawn", lambda cmd: True)
    monkeypatch.setattr(desktop, "running", lambda: None)
    with pytest.raises(AppUnavailable) as e:
        desktop.start(timeout=0.01)
    assert "did not answer" in str(e.value)


def test_probe_asks_the_server_itself(monkeypatch):
    """A live server is the only evidence that counts, so it is really asked."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/local/status"
            body = json.dumps({"version": "9.9.9", "data_dir": "/tmp/emi"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app = desktop.probe(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert app.version == "9.9.9" and app.data_dir == "/tmp/emi"
