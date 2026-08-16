#!/usr/bin/env python3
"""
TikTok domain proxy forwarder — Scrapling-native, CURRENT API.
Config via environment variables. Status server on STATUS_PORT for
container orchestration health checks (Koyeb, Docker, etc.).
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional
from urllib.parse import urlparse

import scrapling
from scrapling.captcha import DetectCaptcha
from scrapling.fetchers import (
    DynamicFetcher,
    DynamicSession,
    Fetcher,
    FetcherSession,
    ProxyRotator,
    StealthyFetcher,
    StealthySession,
)

# ------------------------------------------------------------------ config
DOMAIN    = os.getenv("TARGET_DOMAIN", "api24-normal-useast1a.tiktokv.com")
SCHEME    = os.getenv("TARGET_SCHEME", "https")
API_PATH  = os.getenv("API_PATH", "/")
MODE      = os.getenv("MODE", "adaptive").lower()   # once | loop | adaptive | session
INTERVAL  = float(os.getenv("RUN_INTERVAL", "5"))
TIMEOUT   = int(os.getenv("TIMEOUT", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF   = float(os.getenv("BACKOFF", "2"))
STEALTHY  = os.getenv("STEALTHY", "1") in ("1", "true", "yes")
DYNAMIC   = os.getenv("DYNAMIC", "0") in ("1", "true", "yes")
ROTATE    = os.getenv("PROXY_ROTATE", "0") in ("1", "true", "yes")
CHECK_URL = os.getenv("PROXY_CHECK_URL", "https://httpbin.org/ip")  # "" = skip
STATUS_PORT = int(os.getenv("STATUS_PORT", "8000"))  # 0 = disabled

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tiktok-scrapling")
log.info("Scrapling %s", getattr(scrapling, "__version__", "unknown"))

# ------------------------------------------------------------------ adaptive
# NEW API: adaptive escalation is a class attribute, not per-response method
if STEALTHY:
    StealthyFetcher.adaptive = True
if DYNAMIC:
    DynamicFetcher.adaptive = True

# ------------------------------------------------------------------ proxies
def _split_list(raw: str) -> List[str]:
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

def load_proxies() -> List[str]:
    if raw := os.getenv("PROXY_LIST"):
        return _split_list(raw)
    if url := os.getenv("PROXY_URL"):
        return [url]
    for env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if url := os.getenv(env):
            log.info("Using %s from environment", env)
            return [url]
    return []

def to_playwright_proxy(url: str) -> dict:
    p = urlparse(url)
    d = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
    if p.username:
        d["username"] = p.username
        d["password"] = p.password or ""
    return d

# ------------------------------------------------------------------ engine
def pick_engine(proxy: Optional[str]):
    """Fetcher = HTTP (classmethod .get). Stealthy/Dynamic = browser (.fetch)."""
    if DYNAMIC:
        return DynamicFetcher
    if STEALTHY:
        return StealthyFetcher
    return Fetcher

# ------------------------------------------------------------------ status
STATUS = {"requests": 0, "last_status": None, "proxy": None, "last_error": None,
          "engine": None, "uptime": time.time()}

def _update_status(**kw) -> None:
    STATUS.update(kw)
    STATUS["requests"] += 1

class StatusHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._send(200, {"status": "ok", **STATUS})
        else:
            self._send(404, {"status": "not_found"})

    def log_message(self, *a):
        pass

def start_status_server() -> None:
    if not STATUS_PORT:
        return
    threading.Thread(
        target=HTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler).serve_forever,
        daemon=True,
    ).start()
    log.info("Status server on http://0.0.0.0:%d (health check: /health)", STATUS_PORT)

# ------------------------------------------------------------------ checks
def check_proxy(proxy: str) -> bool:
    if not CHECK_URL:
        return True
    try:
        page = Fetcher.get(CHECK_URL, proxy=proxy, timeout=TIMEOUT)
        ok = page.status == 200
        log.info("Proxy OK [%s] exit IP: %s", proxy,
                 page.text.strip()[:120] if ok else "")
        return ok
    except Exception as exc:
        log.warning("Proxy check failed for %s: %s", proxy, exc)
        return False

def analyze(page) -> None:
    try:
        is_captcha, kind = DetectCaptcha(page)
        if is_captcha:
            log.warning("CAPTCHA/risk wall detected: %s", kind)
    except Exception:
        pass

# ------------------------------------------------------------------ runner
def hit_target(proxy: Optional[str], engine_override=None) -> None:
    url = f"{SCHEME}://{DOMAIN}{API_PATH}"
    Engine = engine_override or pick_engine(proxy)
    browser = Engine is not Fetcher          # Stealthy/Dynamic use .fetch()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if browser:
                kw = {"headless": True}
                if proxy:
                    kw["proxy"] = to_playwright_proxy(proxy)
                page = Engine.fetch(url, **kw)
            else:
                page = Fetcher.get(
                    url,
                    proxy=proxy,
                    timeout=TIMEOUT,
                    stealthy_headers=True,
                    follow_redirects=True,
                )

            analyze(page)
            _update_status(last_status=page.status, proxy=proxy or "direct",
                           engine=Engine.__name__, last_error=None)
            log.info("OK  status=%s engine=%s proxy=%s len=%d",
                     page.status, Engine.__name__, proxy or "direct",
                     len(page.body or b""))
            return
        except Exception as exc:
            wait = BACKOFF * (2 ** (attempt - 1))
            _update_status(last_error=str(exc)[:200], proxy=proxy or "direct",
                           engine=Engine.__name__)
            log.warning("Attempt %d/%d via %s failed: %s (retry in %.1fs)",
                        attempt, MAX_RETRIES, proxy or "direct", exc, wait)
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    log.error("Giving up on %s after %d attempts", url, MAX_RETRIES)

# ------------------------------------------------------------------ modes
def run_once(proxy: Optional[str]) -> None:
    if proxy and not check_proxy(proxy):
        log.error("Proxy rejected: %s", proxy)
        return
    hit_target(proxy)

def run_loop(proxies: List[str]) -> None:
    log.info("Auto-run started: %s -> %s://%s (interval=%.1fs)",
             ", ".join(proxies) if proxies else "DIRECT", SCHEME, DOMAIN, INTERVAL)
    i = 0
    while True:
        run_once(proxies[i % len(proxies)] if proxies else None)
        i += 1
        time.sleep(INTERVAL)

def run_session(proxies: List[str]) -> None:
    Session = (StealthySession if STEALTHY else
               DynamicSession if DYNAMIC else FetcherSession)
    browser = Session is not FetcherSession

    if browser:
        kwargs = {"headless": True}
        if proxies:
            kwargs["proxy"] = to_playwright_proxy(proxies[0])
        if ROTATE:
            kwargs["proxy_rotator"] = ProxyRotator(proxies)
    else:
        kwargs = {"impersonate": "chrome", "timeout": TIMEOUT}
        if proxies:
            kwargs["proxy"] = proxies[0]
        if ROTATE:
            kwargs["proxy_rotator"] = ProxyRotator(proxies)

    with Session(**kwargs) as session:
        log.info("Session started: %s (rotation=%s)", Session.__name__, ROTATE)
        while True:
            try:
                if browser:
                    page = session.fetch(f"{SCHEME}://{DOMAIN}{API_PATH}")
                else:
                    page = session.get(f"{SCHEME}://{DOMAIN}{API_PATH}",
                                       stealthy_headers=True, follow_redirects=True)
                analyze(page)
                _update_status(last_status=page.status, proxy=proxies[0] if proxies else "direct",
                               engine=Session.__name__, last_error=None)
                log.info("OK  status=%s session=%s", page.status, Session.__name__)
            except Exception as exc:
                _update_status(last_error=str(exc)[:200])
                log.warning("Session request failed: %s", exc)
            time.sleep(INTERVAL)

# ------------------------------------------------------------------ main
def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    start_status_server()
    proxies = load_proxies()
    if proxies:
        log.info("Loaded %d proxy(ies)", len(proxies))
    else:
        log.warning("No proxy env vars set - running DIRECT (set PROXY_URL/PROXY_LIST)")

    if MODE == "session":
        run_session(proxies)
    elif MODE == "once":
        run_once(proxies[0] if proxies else None)
    else:  # loop / adaptive
        run_loop(proxies)

if __name__ == "__main__":
    main()
