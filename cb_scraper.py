"""
cb_scraper.py
-------------
The scraping engine.

Strategy (decided after reverse-engineering Crunchbase in May 2026):

  * Crunchbase sits behind Cloudflare. Plain HTTP (requests / urllib) and even
    TLS-impersonating clients (curl_cffi) get a 403 challenge page. A *real*
    browser passes. So we drive a genuine Chrome via `nodriver` - the modern,
    actively-maintained successor to undetected-chromedriver. It clears
    Cloudflare's managed challenge automatically.

  * We never scrape visible HTML. Every Crunchbase page embeds a JSON cache in
    <script id="ng-state">. We read that (see cb_parser.py). It is stable and
    richer than the rendered page.

  * Domain -> Crunchbase slug resolution: try a direct slug guess first (cheap),
    then fall back to a Bing search, then Google. Bing rarely blocks bots.

  * Anti-block hygiene: one warm browser profile reused across the whole run,
    randomised human-like delays, periodic cookie/site-data clearing, and a
    cooldown + browser restart when Cloudflare starts pushing back.

Phase 1 = everything that needs NO login. Funding *total* is login-locked, so
we report "Locked (login required)" for it but still capture round counts.
"""

import asyncio
import os
import random
import re
import sys
import time
from urllib.parse import quote_plus

import nodriver as uc

import cb_parser

# --------------------------------------------------------------------------
# tuning knobs
# --------------------------------------------------------------------------
HEADLESS = False                 # headful is markedly harder for CF to flag
NAV_TIMEOUT = 50                 # seconds for a single navigation
CHALLENGE_TIMEOUT = 60           # seconds to let Cloudflare resolve itself
CF_CLICK_ATTEMPTS = 4            # times to actively click the CF checkbox
MIN_DELAY, MAX_DELAY = 5.0, 11.0 # polite random gap between organizations
RESTART_AFTER_BLOCKS = 2         # consecutive blocks -> cooldown + restart
BLOCK_COOLDOWN = 120             # seconds to wait out a block streak

# NOTE on cookies: we deliberately do NOT wipe cookies mid-run. The Cloudflare
# `cf_clearance` cookie is what proves we already passed a challenge - keeping
# it means later pages load instantly instead of re-challenging. Cookies are
# only cleared as part of block-streak recovery (a fresh start).

def _app_dir():
    """Folder to keep runtime data in - works whether run as a .py or a
    PyInstaller-frozen .exe (in which case it's the folder next to the exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


PROFILE_DIR = os.path.join(_app_dir(), ".browser_profile")

_CHALLENGE_MARKERS = (
    "Just a moment", "Verifying you are human", "challenge-platform",
    "cf-mitigated", "Enable JavaScript and cookies to continue",
)

# common corporate suffixes stripped when guessing a slug from a domain
_SUFFIXES = ("ltd", "limited", "inc", "incorporated", "llc", "corp", "corporation",
             "co", "company", "group", "holdings", "technologies", "technology",
             "tech", "labs", "lab", "software", "solutions", "systems",
             "global", "official", "app", "hq", "the")


def _looks_like_challenge(html):
    return bool(html) and any(m in html for m in _CHALLENGE_MARKERS) \
        and 'id="ng-state"' not in html


# substrings that mean the Chrome/CDP connection itself died (not a block)
_CONN_ERR = ("no close frame", "connection closed", "connection is closed",
             "target closed", "websocket", "cannot connect", "connection refused",
             "browser is closed", "session closed", "connection reset",
             "no inspectable targets", "1006")


def _is_conn_error(exc):
    return any(s in str(exc).lower() for s in _CONN_ERR)


def _parse_proxy(proxy):
    """
    Accept 'host:port', 'http://host:port', 'http://user:pass@host:port' (or
    socks5://...). Returns (server_url, username, password). server_url has no
    credentials - Chrome's --proxy-server cannot carry them inline.
    """
    if not proxy:
        return None, None, None
    proxy = proxy.strip()
    if "://" not in proxy:
        proxy = "http://" + proxy
    parsed = urlparse(proxy)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    return server, parsed.username, parsed.password


class CrunchbaseScraper:
    """Owns one Chrome instance and knows how to pull Crunchbase data."""

    def __init__(self, log=print, proxy=None):
        self.browser = None
        self.log = log
        self.proxy = proxy or None        # 'host:port' or 'http://user:pass@host:port'
        self._block_streak = 0

    # --- lifecycle ---------------------------------------------------------

    async def start(self):
        if self.browser is not None:
            return
        self.log("Launching browser...")
        os.makedirs(PROFILE_DIR, exist_ok=True)

        args = []
        server, user, pw = _parse_proxy(self.proxy)
        if server:
            args.append(f"--proxy-server={server}")
            self.log(f"Routing through proxy: {server}")

        self.browser = await uc.start(
            headless=HEADLESS,
            user_data_dir=PROFILE_DIR,    # warm, persistent profile builds trust
            browser_args=args,
        )
        if server and user:
            await self._install_proxy_auth(user, pw)
        self.log("Browser ready.")

    async def _install_proxy_auth(self, user, pw):
        """Answer the proxy's Basic-auth challenge over CDP (per main tab)."""
        from nodriver import cdp
        tab = self.browser.main_tab

        async def on_auth(event):
            await tab.send(cdp.fetch.continue_with_auth(
                request_id=event.request_id,
                auth_challenge_response=cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials", username=user, password=pw),
            ))

        async def on_paused(event):
            await tab.send(cdp.fetch.continue_request(request_id=event.request_id))

        tab.add_handler(cdp.fetch.AuthRequired, on_auth)
        tab.add_handler(cdp.fetch.RequestPaused, on_paused)
        await tab.send(cdp.fetch.enable(handle_auth_requests=True))
        self.log("Proxy authentication handler installed.")

    async def stop(self):
        if self.browser is None:
            return
        try:
            res = self.browser.stop()
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass
        self.browser = None

    async def restart(self):
        self.log("Restarting browser...")
        await self.stop()
        await asyncio.sleep(2)
        await self.start()

    async def _clear_cookies(self):
        try:
            res = self.browser.cookies.clear()
            if asyncio.iscoroutine(res):
                await res
            self.log("Cleared cookies / site data.")
        except Exception as e:
            self.log(f"(cookie clear skipped: {e})")

    # --- low-level fetch ---------------------------------------------------

    async def _navigate(self, url, expect):
        """One navigation attempt. Returns HTML, or raises (block / conn loss)."""
        tab = await asyncio.wait_for(self.browser.get(url), timeout=NAV_TIMEOUT)

        deadline = time.time() + CHALLENGE_TIMEOUT
        clicks = 0
        html = ""
        while time.time() < deadline:
            await asyncio.sleep(2.5)
            try:
                html = await tab.get_content()
            except Exception as e:
                if _is_conn_error(e):
                    raise            # browser died - let fetch() restart it
                continue             # transient - try again
            if not _looks_like_challenge(html) and (
                    expect is None or expect in html or len(html) > 60_000):
                self._block_streak = 0
                return html
            # interactive challenge present - try to click the checkbox
            if clicks < CF_CLICK_ATTEMPTS:
                clicks += 1
                try:
                    await tab.verify_cf()
                    await asyncio.sleep(3.0)
                except Exception:
                    pass  # no visible checkbox yet (still silent challenge)
        # timed out on a challenge
        self._block_streak += 1
        raise RuntimeError("Cloudflare challenge did not clear")

    async def fetch(self, url, expect="ng-state"):
        """
        Navigate to `url` and return page HTML once it is genuinely loaded.

        nodriver clears Cloudflare's *silent* JS challenge on its own. When
        Cloudflare escalates to the *interactive* "Verify you are human"
        checkbox, we actively click it via `verify_cf()`. If the browser's CDP
        connection drops mid-run, the browser is relaunched and the fetch is
        retried once. Raises RuntimeError if Cloudflare never clears.
        """
        await self.start()
        for attempt in (1, 2):
            try:
                return await self._navigate(url, expect)
            except RuntimeError:
                raise                       # genuine Cloudflare block
            except Exception as e:
                if _is_conn_error(e) and attempt == 1:
                    self.log(f"  browser connection lost ({e}); relaunching")
                    await self.restart()
                    continue
                raise

    async def _polite_pause(self):
        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    async def _maybe_maintenance(self):
        """Block-streak recovery between organizations."""
        if self._block_streak >= RESTART_AFTER_BLOCKS:
            self.log(f"Block streak detected - cooling down {BLOCK_COOLDOWN}s, "
                     f"clearing cookies and restarting browser.")
            await asyncio.sleep(BLOCK_COOLDOWN)
            await self._clear_cookies()
            await self.restart()
            self._block_streak = 0

    # --- domain -> slug resolution ----------------------------------------

    @staticmethod
    def _slug_candidates(domain):
        """Plausible Crunchbase permalinks derived straight from a domain."""
        sld = cb_parser.domain_of(domain).split(".")[0]
        cands = [sld]
        low = sld.lower()
        for suf in sorted(_SUFFIXES, key=len, reverse=True):
            if low.endswith(suf) and len(low) > len(suf) + 1:
                cands.append(low[:-len(suf)])
        # de-dupe, keep order
        seen, out = set(), []
        for c in cands:
            c = re.sub(r"[^a-z0-9-]", "", c.lower())
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    @staticmethod
    def _crunchbase_links(html):
        """All organization permalinks referenced in a search results page."""
        found = re.findall(r"crunchbase\.com/organization/([a-z0-9][a-z0-9._-]*)",
                            html or "", re.I)
        seen, out = set(), []
        for slug in found:
            slug = slug.rstrip(".-").lower()
            if slug and slug not in seen and not slug.startswith("organization"):
                seen.add(slug)
                out.append(slug)
        return out

    async def _search_engine_slug(self, domain):
        """Ask Bing (then Google) which Crunchbase org page matches a domain."""
        query = f'"{domain}" crunchbase'
        engines = [
            ("Bing", f"https://www.bing.com/search?q={quote_plus(query)}"),
            ("Google", f"https://www.google.com/search?q={quote_plus(query)}"),
        ]
        for name, url in engines:
            try:
                self.log(f"  searching {name} for {domain}")
                html = await self.fetch(url, expect=None)
                links = self._crunchbase_links(html)
                if links:
                    self.log(f"  {name} -> {links[0]}")
                    return links[0]
            except Exception as e:
                self.log(f"  {name} search failed: {e}")
            await self._polite_pause()
        return None

    async def resolve(self, domain):
        """
        Resolve a domain to (permalink, org_html). org_html is returned when we
        already loaded the right page during resolution, to avoid a 2nd fetch.
        Returns (None, None) if nothing matched.
        """
        domain = cb_parser.domain_of(domain) or domain.strip().lower()

        # 1) cheap direct guess - verify the org's website matches the domain
        for slug in self._slug_candidates(domain):
            url = f"https://www.crunchbase.com/organization/{slug}"
            try:
                html = await self.fetch(url)
            except Exception as e:
                self.log(f"  guess '{slug}' failed: {e}")
                continue
            rec = cb_parser.parse_organization(html)
            if not rec:
                self.log(f"  guess '{slug}': no Crunchbase page there")
                continue
            site = cb_parser.domain_of(rec.get("website"))
            if site and (site == domain or site.endswith("." + domain)
                         or domain.endswith("." + site)):
                self.log(f"  matched by direct guess: {slug}")
                return rec.get("crunchbase_permalink") or slug, html
            self.log(f"  guess '{slug}' is a different company ({site or '?'})")

        # 2) search engines
        slug = await self._search_engine_slug(domain)
        if not slug:
            return None, None

        # load the searched org page; one reload retry if parsing fails
        for attempt in (1, 2):
            try:
                html = await self.fetch(
                    f"https://www.crunchbase.com/organization/{slug}")
            except Exception as e:
                self.log(f"  loading '{slug}' failed: {e}")
                return None, None
            rec = cb_parser.parse_organization(html)
            if rec:
                return rec.get("crunchbase_permalink") or slug, html
            self.log(f"  '{slug}' loaded but org data not parsed "
                     f"(html={len(html)}, ng-state={'ng-state' in html}, "
                     f"attempt {attempt})")
            await self._polite_pause()
        return None, None

    # --- person enrichment -------------------------------------------------

    async def enrich_people(self, people, max_people):
        """Visit individual person pages to pull socials / website / email."""
        for person in people[:max_people]:
            if not person.get("crunchbase_url"):
                continue
            try:
                await self._polite_pause()
                html = await self.fetch(person["crunchbase_url"])
                extra = cb_parser.parse_person(html)
                for k, v in extra.items():
                    person[k] = v
                self.log(f"    + person: {person['name']}")
            except Exception as e:
                self.log(f"    person '{person['name']}' failed: {e}")

    # --- the public per-domain entry point --------------------------------

    async def scrape_domain(self, domain, enrich=False, max_people=5):
        """
        Full pipeline for one domain. Returns a result dict with keys:
        status ('done' | 'not_found' | 'error'), record (or None), error.
        """
        await self._maybe_maintenance()
        try:
            permalink, html = await self.resolve(domain)
            if not permalink or not html:
                return {"status": "not_found", "record": None,
                        "error": "No matching Crunchbase organization found"}

            record = cb_parser.parse_organization(html)
            if not record:
                return {"status": "error", "record": None,
                        "error": "Found page but could not parse data"}

            record["input_domain"] = domain
            if enrich and record.get("key_people"):
                await self.enrich_people(record["key_people"], max_people)

            return {"status": "done", "record": record, "error": None}
        except Exception as e:
            return {"status": "error", "record": None, "error": str(e)}
        finally:
            await self._polite_pause()
