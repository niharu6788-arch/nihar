import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ticket_watch as tw  # noqa: E402

PAGES = {
    "/sold-out": (200, "<h1>Chelsea v Test FC</h1><p>Sold out. No tickets available right now.</p>"),
    "/on-sale": (200, "<h1>Chelsea v Test FC</h1><button id='buy'>Buy tickets</button><p>Seats on sale now.</p>"),
    "/logged-out": (200, "<h1>Please sign in to your account to continue</h1>"),
    "/rate-limited": (429, "<p>Too many requests, please slow down and try later.</p>"),
    "/queue-it/wait": (200, "<p>You are now in the queue. Please wait for your turn.</p>"),
}
HITS = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        HITS.append(self.path)
        if self.path == "/to-queue":
            self.send_response(302)
            self.send_header("Location", "/queue-it/wait")
            self.end_headers()
            return
        status, html = PAGES.get(self.path, (404, "<p>not found on this test server</p>"))
        body = f"<html><body>{html}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        kwargs = {}
        if os.environ.get("CHROMIUM_PATH"):
            kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, message, url=None, urgent=False):
        self.sent.append((title, urgent))


def make_watch(url, condition="text_absent", value=("sold out",), logged_out=("please sign in",)):
    value = value if condition == "selector" else list(value)
    return tw.Watch("Test match", url, condition, value, list(logged_out))


def make_cfg(watch, tmp_path):
    return tw.Config(watches=[watch], screenshot_dir=tmp_path, sound=False)


def test_text_absent(page, base_url, tmp_path):
    watch = make_watch(base_url + "/sold-out")
    cfg = make_cfg(watch, tmp_path)
    assert tw.check(watch, page, cfg).state == "unavailable"
    watch.url = base_url + "/on-sale"
    assert tw.check(watch, page, cfg).state == "available"


def test_text_and_selector(page, base_url, tmp_path):
    for condition, value in (("text", ["on sale now"]), ("selector", "#buy")):
        watch = make_watch(base_url + "/on-sale", condition, value)
        cfg = make_cfg(watch, tmp_path)
        assert tw.check(watch, page, cfg).state == "available"
        watch.url = base_url + "/sold-out"
        assert tw.check(watch, page, cfg).state == "unavailable"


def test_logged_out_and_blocked(page, base_url, tmp_path):
    watch = make_watch(base_url + "/logged-out")
    cfg = make_cfg(watch, tmp_path)
    assert tw.check(watch, page, cfg).state == "logged_out"
    watch.url = base_url + "/rate-limited"
    result = tw.check(watch, page, cfg)
    assert (result.state, result.status) == ("blocked", 429)


def test_queue_is_never_reloaded(page, base_url, tmp_path):
    watch = make_watch(base_url + "/to-queue")
    cfg = make_cfg(watch, tmp_path)
    watch.page = page
    notifier = FakeNotifier()
    tw.react(watch, tw.check(watch, page, cfg), cfg, notifier)
    assert watch.state == "queued"
    hits_before = len(HITS)
    tw.react(watch, tw.check(watch, page, cfg), cfg, notifier)
    assert watch.state == "queued"
    assert len(HITS) == hits_before
    assert len(notifier.sent) == 1


def test_available_hands_tab_over(page, base_url, tmp_path):
    watch = make_watch(base_url + "/on-sale")
    cfg = make_cfg(watch, tmp_path)
    watch.page = page
    notifier = FakeNotifier()
    tw.react(watch, tw.check(watch, page, cfg), cfg, notifier, now=1000)
    assert watch.page is None
    assert watch.next_check == 1000 + cfg.hold_minutes * 60
    assert notifier.sent == [("TICKETS: Test match", True)]
    assert list(tmp_path.glob("*.png"))


def test_blocked_backs_off(tmp_path):
    watch = make_watch("https://example.invalid")
    cfg = make_cfg(watch, tmp_path)
    notifier = FakeNotifier()
    tw.react(watch, tw.Result("blocked", "HTTP 429", 429), cfg, notifier, now=0)
    first = watch.next_check
    tw.react(watch, tw.Result("blocked", "HTTP 429", 429), cfg, notifier, now=0)
    assert watch.next_check > first > cfg.interval
    assert len(notifier.sent) == 1


def test_config_enforces_minimum_interval(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "interval_seconds: 0.001\n"
        "watches:\n"
        "  - name: Test\n"
        "    url: https://example.com\n"
        "    available_when:\n"
        "      text_absent: Sold out\n"
    )
    cfg = tw.load_config(path)
    assert cfg.interval == tw.MIN_INTERVAL_SECONDS
    assert cfg.watches[0].condition_value == ["Sold out"]
