#!/usr/bin/env python3
"""
Auto-run proxy forwarder for TikTok API domains via Scrapling.
All configuration comes from environment variables (see ENV VARS table below).

Modes:
  MODE=once     -> single request, exit
  MODE=loop     -> run forever at RUN_INTERVAL seconds (default)
  MODE=session  -> persistent session with proxy rotation
"""

import logging
import os
import signal
import sys
import time
from typing import List, Optional

from scrapling.fetchers import Fetcher, FetcherSession, ProxyRotator, StealthyFetcher

# ---------------------------------------------------------------- config
DOMAIN   = os.getenv("TARGET_DOMAIN", "api24-normal-useast1a.tiktokv.com")
SCHEME   = os.getenv("TARGET_SCHEME", "https")
API_PATH = os.getenv("API_PATH", "/")                     # e.g. /aweme/v1/web/follow/feed/
MODE     = os.getenv("MODE", "loop").lower()              # once | loop | session
INTERVAL = float(os.getenv("RUN_INTERVAL", "5"))
TIMEOUT  = int(os.getenv("TIMEOUT", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF  = float(os.getenv("BACKOFF", "2"))               # seconds, doubles per retry
STEALTHY = os.getenv("STEALTHY", "0") in ("1", "true", "yes")
ROTATE   = os.getenv("PROXY_ROTATE", "0") in ("1", "true", "yes")
CHECK_URL = os.getenv("PROXY_CHECK_URL", "https://httpbin.org/ip")  # "" disables

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tiktok-proxy")

# ---------------------------------------------------------------- proxies
def _split_list(raw: str) -> List[str]:
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

def load_proxies() -> List[str]:
    """Priority: PROXY_LIST > PROXY_URL > HTTPS_PROXY/HTTP_PROXY."""
    if raw := os.getenv("PROXY_LIST"):
        return _split_list(raw)
    if url := os.getenv("PROXY_URL"):
        return [url]
    for env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if url := os.getenv(env):
            log.info("Using %s from environment", env)
            return [url]
    return []

# ---------------------------------------------------------------- engine
def make_fetcher(proxy: Optional[str] = None):
    """Return a one-shot fetcher bound to the chosen engine."""
    kwargs = {
        "proxy": proxy,
        "timeout": TIMEOUT,
        "stealthy_headers": True,
        "follow_redirects": True,
    }
    if STEALTHY:
        return StealthyFetcher.get if False else StealthyFetcher  # (see note below)
    return Fetcher

def check_proxy(proxy: str) -> bool:
    """Confirm the proxy actually works and report its exit IP."""
    if not CHECK_URL:
        return True
    try:
        page = Fetcher.get(CHECK_URL, proxy=proxy, timeout=TIMEOUT)
        ok = page.status == 200
        log.info("Proxy OK [%s] exit IP: %s", proxy, page.text.strip()[:120] if ok else "")
        return ok
    except Exception as exc:
        log.warning("Proxy check failed for %s: %s", proxy, exc)
        return False

def hit_target(proxy: Optional[str]) -> None:
    """Send one request to the TikTok domain through the proxy."""
    url = f"{SCHEME}://{DOMAIN}{API_PATH}"
    engine = StealthyFetcher if STEALTHY else Fetcher
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page = engine.get(
                url,
                proxy=proxy,
                timeout=TIMEOUT,
                stealthy_headers=True,
                follow_redirects=True,
            )
            log.info(
                "OK  status=%s proxy=%s len=%d bytes",
                page.status, proxy or "direct", len(page.body or b""),
            )
            return
        except Exception as exc:
            wait = BACKOFF * (2 ** (attempt - 1))
            log.warning(
                "Attempt %d/%d failed via %s: %s (retry in %.1fs)",
                attempt, MAX_RETRIES, proxy or "direct", exc, wait,
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    log.error("Giving up on %s after %d attempts", url, MAX_RETRIES)

# ---------------------------------------------------------------- runners
def run_once(proxy: Optional[str] = None) -> None:
    if proxy and not check_proxy(proxy):
        log.error("Proxy rejected: %s", proxy)
        return
    hit_target(proxy)

def run_loop(proxies: List[str]) -> None:
    log.info("Auto-run started: %d proxy(ies) -> %s://%s (interval=%.1fs)",
             len(proxies) or 1, SCHEME, DOMAIN, INTERVAL)
    i = 0
    while True:
        proxy = proxies[i % len(proxies)] if proxies else None
        run_once(proxy)
        i += 1
        time.sleep(INTERVAL)

def run_session(proxies: List[str]) -> None:
    """Persistent session; rotates proxy per request if PROXY_ROTATE=1."""
    kwargs = {"impersonate": "chrome", "timeout": TIMEOUT}
    if proxies:
        kwargs["proxy"] = proxies[0]
        if ROTATE:
            kwargs["proxy_rotator"] = ProxyRotator(proxies)
    with FetcherSession(**kwargs) as session:
        log.info("Session started (rotation=%s)", ROTATE)
        while True:
            try:
                page = session.get(
                    f"{SCHEME}://{DOMAIN}{API_PATH}",
                    stealthy_headers=True,
                    follow_redirects=True,
                )
                log.info("OK  status=%s", page.status)
            except Exception as exc:
                log.warning("Session request failed: %s", exc)
            time.sleep(INTERVAL)

# ---------------------------------------------------------------- main
def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    proxies = load_proxies()
    if proxies:
        log.info("Loaded %d proxy(ies)", len(proxies))
    else:
        log.warning("No proxy env vars set — running DIRECT (set PROXY_URL/PROXY_LIST)")

    if MODE == "session":
        run_session(proxies)
    elif MODE == "once":
        run_once(proxies[0] if proxies else None)
    else:
        run_loop(proxies)

if __name__ == "__main__":
    main()
