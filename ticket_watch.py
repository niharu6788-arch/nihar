#!/usr/bin/env python3
"""Ticket availability watcher.

Reloads ticket pages you are logged in to at a polite interval and alerts you
(phone push via ntfy, terminal beep) the moment tickets look available. If you
recorded basket steps, it clicks through them once to put tickets in your
basket, then you pay yourself in the browser window it already has open.

It deliberately never pays or checks out, never solves CAPTCHAs and never
skips or refreshes queues.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / "browser-profile"
SCREENSHOT_DIR = HERE / "screenshots"

MIN_INTERVAL_SECONDS = 10
DEFAULT_INTERVAL_SECONDS = 30
QUEUE_POLL_SECONDS = 5
LOGGED_OUT_RECHECK_SECONDS = 120
MAX_BACKOFF_SECONDS = 15 * 60
STEP_TIMEOUT_MS = 15_000
CONDITION_KEYS = ("selector", "text", "text_absent")
STEP_KINDS = ("click", "select", "wait_for")
# Basket steps must stop before payment, so refuse anything that looks like a checkout button.
PAYMENT_RE = re.compile(
    r"\b(pay|payment|pay now|checkout|check out|place order|complete purchase|confirm purchase|confirm order)\b",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def first_line(err: Exception) -> str:
    return (str(err).splitlines() or [type(err).__name__])[0]


@dataclass
class Step:
    kind: str  # one of STEP_KINDS
    selector: str
    value: str | None = None  # option to pick, for "select" steps


@dataclass
class Watch:
    name: str
    url: str
    condition: str  # one of CONDITION_KEYS
    condition_value: str | list[str]
    logged_out_text: list[str] = field(default_factory=list)
    basket_steps: list[Step] = field(default_factory=list)
    basket_tried: bool = False
    state: str = "new"
    next_check: float = 0.0
    failures: int = 0
    page: object = None


@dataclass
class Config:
    watches: list[Watch]
    interval: float = DEFAULT_INTERVAL_SECONDS
    jitter: float = 5.0
    hold_minutes: float = 10.0
    headless: bool = False
    queue_url_markers: list[str] = field(default_factory=lambda: ["queue-it"])
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    sound: bool = True
    screenshot_dir: Path = SCREENSHOT_DIR


@dataclass
class Result:
    state: str  # available | unavailable | queued | logged_out | blocked | error
    detail: str
    status: int | None = None


def parse_steps(name: str, raw_steps) -> list[Step]:
    steps = []
    for i, raw in enumerate(raw_steps or [], start=1):
        kinds = [k for k in STEP_KINDS if k in raw] if isinstance(raw, dict) else []
        if len(kinds) != 1:
            sys.exit(f"Watch '{name}': basket step {i} needs exactly one of {', '.join(STEP_KINDS)}.")
        kind = kinds[0]
        if kind == "select":
            opts = raw["select"] if isinstance(raw["select"], dict) else {}
            step = Step(kind, str(opts.get("selector") or ""), str(opts.get("value") or ""))
            if not step.selector or not step.value:
                sys.exit(f"Watch '{name}': basket step {i} select needs both selector and value.")
        else:
            step = Step(kind, str(raw[kind] or ""))
            if not step.selector:
                sys.exit(f"Watch '{name}': basket step {i} has an empty selector.")
        if PAYMENT_RE.search(step.selector):
            sys.exit(
                f"Watch '{name}': basket step {i} ({step.selector!r}) looks like a payment or checkout step. "
                "The bot only fills the basket, you pay yourself."
            )
        steps.append(step)
    return steps


def load_config(path: Path) -> Config:
    if not path.exists():
        sys.exit(f"Config file {path} not found. Copy config.example.yaml to config.yaml and edit it.")
    raw = yaml.safe_load(path.read_text()) or {}

    interval = float(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
    if interval < MIN_INTERVAL_SECONDS:
        log(
            f"interval_seconds={interval:g} is below the {MIN_INTERVAL_SECONDS}s minimum, using "
            f"{MIN_INTERVAL_SECONDS}s. Faster polling gets accounts blocked and gains nothing, "
            "a single page load already takes around a second."
        )
        interval = MIN_INTERVAL_SECONDS

    watches = []
    for i, w in enumerate(raw.get("watches") or [], start=1):
        name = w.get("name") or f"watch {i}"
        url = str(w.get("url") or "")
        if not url.startswith(("http://", "https://")):
            sys.exit(f"Watch '{name}': url must be a full http(s) link, got {url!r}.")
        cond = w.get("available_when") or {}
        keys = [k for k in CONDITION_KEYS if k in cond]
        if len(keys) != 1:
            sys.exit(f"Watch '{name}': available_when needs exactly one of {', '.join(CONDITION_KEYS)}.")
        key = keys[0]
        value = str(cond[key]) if key == "selector" else as_list(cond[key])
        logged_out = as_list((w.get("logged_out_when") or {}).get("text"))
        watches.append(Watch(name, url, key, value, logged_out, parse_steps(name, w.get("basket_steps"))))
    if not watches:
        sys.exit(f"No watches configured in {path}.")

    notify = raw.get("notify") or {}
    return Config(
        watches=watches,
        interval=interval,
        jitter=max(0.0, float(raw.get("jitter_seconds", 5))),
        hold_minutes=max(0.0, float(raw.get("hold_minutes", 10))),
        headless=bool(raw.get("headless", False)),
        queue_url_markers=as_list(raw.get("queue_url_markers", ["queue-it"])),
        ntfy_topic=str(notify.get("ntfy_topic") or ""),
        ntfy_server=str(notify.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
        sound=bool(notify.get("sound", True)),
    )


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, title: str, message: str, url: str | None = None, urgent: bool = False) -> None:
        log(f"ALERT: {title}: {message}")
        if self.cfg.sound:
            sys.stdout.write("\a\a\a" if urgent else "\a")
            sys.stdout.flush()
        if not self.cfg.ntfy_topic:
            return
        payload = {
            "topic": self.cfg.ntfy_topic,
            "title": title,
            "message": message,
            "priority": 5 if urgent else 3,
            "tags": ["tickets"],
        }
        if url:
            payload["click"] = url
        try:
            requests.post(self.cfg.ntfy_server, json=payload, timeout=10).raise_for_status()
        except requests.RequestException as e:
            log(f"Could not send phone notification: {e}")


def open_browser(p, headless: bool, profile_dir: Path = PROFILE_DIR):
    kwargs = {"headless": headless}
    if os.environ.get("CHROMIUM_PATH"):
        kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
    if not headless:
        kwargs["no_viewport"] = True
    profile_dir.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)


def first_match(body: str, needles: list[str]) -> str | None:
    for needle in needles:
        if needle.lower() in body:
            return needle
    return None


def in_queue(page, cfg: Config) -> bool:
    url = page.url.lower()
    return any(m.lower() in url for m in cfg.queue_url_markers)


def evaluate(watch: Watch, page, body: str) -> tuple[bool, str]:
    if watch.condition == "selector":
        loc = page.locator(watch.condition_value)
        visible = loc.count() > 0 and loc.first.is_visible()
        return visible, f"selector {watch.condition_value!r} {'is visible' if visible else 'not on page'}"
    hit = first_match(body, watch.condition_value)
    if watch.condition == "text":
        return hit is not None, f'found "{hit}"' if hit else "none of the on-sale phrases on page"
    return hit is None, f'"{hit}" still on page' if hit else "none of the sold-out phrases on page"


def check(watch: Watch, page, cfg: Config) -> Result:
    status = None
    if watch.state == "queued" and in_queue(page, cfg):
        # Never reload a queue page, that can lose your place in it.
        return Result("queued", page.url)
    if watch.state != "queued":
        try:
            response = page.goto(watch.url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeout:
            return Result("error", "page took longer than 45s to load")
        except PlaywrightError as e:
            return Result("error", first_line(e))
        status = response.status if response else None
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeout:
            pass  # some ticket pages never go fully idle, what has rendered is enough
        if in_queue(page, cfg):
            return Result("queued", page.url, status)
        if status in (403, 429):
            return Result("blocked", f"HTTP {status}", status)
        if status is not None and status >= 400:
            return Result("error", f"HTTP {status}", status)

    try:
        body = page.inner_text("body").lower()
        if len(body.strip()) < 20:
            return Result("error", "page looks empty, not trusting it", status)
        hit = first_match(body, watch.logged_out_text)
        if hit:
            return Result("logged_out", f'found "{hit}"', status)
        available, detail = evaluate(watch, page, body)
    except PlaywrightError as e:
        return Result("error", f"could not read page: {first_line(e)}", status)
    return Result("available" if available else "unavailable", detail, status)


def fill_basket(watch: Watch, page) -> tuple[bool, str]:
    for i, step in enumerate(watch.basket_steps, start=1):
        loc = page.locator(step.selector).first
        try:
            if step.kind == "click":
                loc.click(timeout=STEP_TIMEOUT_MS)
            elif step.kind == "select":
                loc.select_option(step.value, timeout=STEP_TIMEOUT_MS)
            else:
                loc.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        except PlaywrightError as e:
            return False, f"step {i} ({step.kind} {step.selector!r}) failed: {first_line(e)}"
    return True, f"all {len(watch.basket_steps)} steps done"


def save_screenshot(page, watch: Watch, directory: Path) -> Path | None:
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", watch.name.lower()).strip("-") or "watch"
    path = directory / f"{slug}-{datetime.now():%Y%m%d-%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return path
    except PlaywrightError:
        return None


def bring_to_front(page) -> None:
    try:
        page.bring_to_front()
    except PlaywrightError:
        pass


def react(watch: Watch, result: Result, cfg: Config, notifier: Notifier, now: float | None = None) -> None:
    now = time.time() if now is None else now
    prev, watch.state = watch.state, result.state
    if not (prev == result.state == "queued"):
        log(f"{watch.name}: {result.state} ({result.detail})")

    if result.state == "available":
        watch.failures = 0
        bring_to_front(watch.page)
        if watch.basket_steps and not watch.basket_tried:
            # Only one attempt per match per run, so it can never keep piling tickets into the basket.
            watch.basket_tried = True
            notifier.send(f"TICKETS: {watch.name}", "Looks available, filling your basket now.", watch.url, urgent=True)
            ok, detail = fill_basket(watch, watch.page)
            log(f"{watch.name}: basket {'filled' if ok else 'not filled'} ({detail})")
            if ok:
                title, message = f"IN BASKET: {watch.name}", "Pay now in the bot's browser window before the hold runs out."
            else:
                title, message = (
                    f"FINISH IT YOURSELF: {watch.name}",
                    f"Filling the basket failed, {detail}. If there is a robot check, solve it in the bot's window.",
                )
        else:
            title, message = f"TICKETS: {watch.name}", f"Looks available ({result.detail}). Buy now in the bot's window."
        save_screenshot(watch.page, watch, cfg.screenshot_dir)
        notifier.send(title, message, watch.url, urgent=True)
        # Hand this tab over to you: it is never reloaded again, later checks use a new tab.
        watch.page = None
        watch.next_check = now + cfg.hold_minutes * 60
        log(f"{watch.name}: tab is yours, next check in {cfg.hold_minutes:g} min in a new tab")
        return

    if result.state in ("blocked", "error"):
        watch.failures += 1
        delay = min(cfg.interval * 2 ** watch.failures, MAX_BACKOFF_SECONDS)
        if result.state == "blocked" and prev != "blocked":
            notifier.send(
                f"Slowing down: {watch.name}",
                f"The site refused the request ({result.detail}), backing off so your account is not flagged.",
            )
        watch.next_check = now + delay
        log(f"{watch.name}: retrying in {delay:.0f}s")
        return

    watch.failures = 0
    if result.state == "queued":
        if prev != "queued":
            bring_to_front(watch.page)
            notifier.send(
                f"In queue: {watch.name}",
                "You're in the site's queue. The bot will not refresh it, keep the window open.",
                watch.url,
                urgent=True,
            )
        watch.next_check = now + QUEUE_POLL_SECONDS
    elif result.state == "logged_out":
        if prev != "logged_out":
            bring_to_front(watch.page)
            notifier.send(f"Logged out: {watch.name}", "Log back in inside the bot's browser window.", watch.url)
        watch.next_check = now + LOGGED_OUT_RECHECK_SECONDS
    else:
        watch.next_check = now + cfg.interval + random.uniform(0, cfg.jitter)


def run_watch(cfg: Config, profile_dir: Path = PROFILE_DIR) -> None:
    notifier = Notifier(cfg)
    with sync_playwright() as p:
        context = open_browser(p, cfg.headless, profile_dir)
        spare_tabs = list(context.pages)
        log(f"Watching {len(cfg.watches)} page(s) every ~{cfg.interval:g}s. Ctrl+C to stop.")
        try:
            while True:
                watch = min(cfg.watches, key=lambda w: w.next_check)
                wait = watch.next_check - time.time()
                if wait > 0:
                    time.sleep(wait)
                if watch.page is None or watch.page.is_closed():
                    watch.page = spare_tabs.pop() if spare_tabs else context.new_page()
                    if watch.state == "queued":
                        watch.state = "new"  # the queue tab is gone, start over with a normal load
                react(watch, check(watch, watch.page, cfg), cfg, notifier)
        except KeyboardInterrupt:
            log("Stopped.")
        except PlaywrightError as e:
            log(f"Browser closed or crashed ({first_line(e)}), stopping.")
        finally:
            try:
                context.close()
            except Exception:  # Ctrl+C also kills the Playwright driver, so the browser may already be gone
                pass


def run_test(cfg: Config, profile_dir: Path = PROFILE_DIR) -> None:
    with sync_playwright() as p:
        context = open_browser(p, cfg.headless, profile_dir)
        try:
            for watch in cfg.watches:
                page = context.new_page()
                result = check(watch, page, cfg)
                shot = save_screenshot(page, watch, cfg.screenshot_dir)
                title = page.title() if not page.is_closed() else ""
                print(f"\n[{watch.name}]")
                print(f"  final URL : {page.url}")
                print(f"  HTTP      : {result.status}")
                print(f"  title     : {title}")
                print(f"  result    : {result.state} ({result.detail})")
                print(f"  screenshot: {shot}")
        finally:
            context.close()


def run_test_basket(cfg: Config, profile_dir: Path = PROFILE_DIR) -> None:
    watch = next((w for w in cfg.watches if w.basket_steps), None)
    if watch is None:
        sys.exit("No watch has basket_steps in the config.")
    with sync_playwright() as p:
        context = open_browser(p, headless=False, profile_dir=profile_dir)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(watch.url, wait_until="domcontentloaded", timeout=45_000)
            ok, detail = fill_basket(watch, page)
            log(f"{watch.name}: basket {'filled' if ok else 'not filled'} ({detail})")
            input("Check the window (empty the basket if you don't want them), then press Enter to close... ")
        finally:
            try:
                context.close()
            except Exception:
                pass


def run_record(url: str, profile_dir: Path = PROFILE_DIR) -> None:
    log("Click through to 'Add to basket' in the window that opens. Each click shows up as code in the "
        "Playwright Inspector window, copy it into basket_steps (the README shows how).")
    subprocess.run(
        [sys.executable, "-m", "playwright", "codegen", "--target=python", f"--user-data-dir={profile_dir}", url],
        check=False,
    )


def run_login(url: str, profile_dir: Path = PROFILE_DIR) -> None:
    with sync_playwright() as p:
        context = open_browser(p, headless=False, profile_dir=profile_dir)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)
        input("Log in inside the browser window, then press Enter here to save the session... ")
        context.close()
    log(f"Session saved in {profile_dir}. Now run: python ticket_watch.py test")


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch ticket pages and alert you when tickets appear.")
    parser.add_argument("-c", "--config", default=str(HERE / "config.yaml"), help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="open the browser so you can log in once, the session is kept")
    login.add_argument("--url", help="page to open, defaults to the first watch url")
    record = sub.add_parser("record", help="record your clicks to 'Add to basket' as code for basket_steps")
    record.add_argument("--url", help="page to start on, defaults to the first watch url")
    sub.add_parser("test", help="check every watch once and print what the bot sees")
    sub.add_parser("test-basket", help="run the basket steps once right now, to check they work")
    sub.add_parser("test-notify", help="send a test alert to your phone")
    sub.add_parser("watch", help="keep checking and alert you when tickets appear")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.command == "login":
        run_login(args.url or cfg.watches[0].url)
    elif args.command == "record":
        run_record(args.url or cfg.watches[0].url)
    elif args.command == "test":
        run_test(cfg)
    elif args.command == "test-basket":
        run_test_basket(cfg)
    elif args.command == "test-notify":
        Notifier(cfg).send("Ticket bot test", "If you can read this on your phone, alerts work.", urgent=True)
        if not cfg.ntfy_topic:
            log("No ntfy_topic set in the config, so only the terminal beep was used.")
    else:
        run_watch(cfg)


if __name__ == "__main__":
    main()
