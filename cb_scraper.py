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
    then fall back to a Google search, then Bing. Google resolves the right org
    page more often, so trying it first usually avoids the second search.

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
from urllib.parse import quote_plus, urlparse

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

# --------------------------------------------------------------------------
# Phase 2: logged-in session
# --------------------------------------------------------------------------
CB_BASE = "https://www.crunchbase.com"
CB_LOGIN_URL = "https://www.crunchbase.com/login"

# Telemetry / usage-counter endpoints to block. KEY INSIGHT: every field we
# collect is server-rendered into ng-state in the FIRST document response, so
# blocking these *later* calls removes ZERO data - it only stops the request
# that charges a profile view against a free account's daily quota. The generic
# analytics patterns below are safe to block always. Crunchbase's own usage
# counter is account/version-specific: run with diagnostic=True, watch the
# logged request list for the call that fires right as the page settles, then
# add its URL pattern here.
DEFAULT_BLOCKED_URLS = [
    "*google-analytics.com*", "*googletagmanager.com*", "*analytics.google.com*",
    "*segment.io*", "*segment.com*", "*cdn.segment.com*",
    "*heapanalytics.com*", "*heap.io*", "*mixpanel.com*", "*amplitude.com*",
    "*doubleclick.net*", "*facebook.net*", "*facebook.com/tr*",
    "*hotjar.com*", "*hotjar.io*", "*fullstory.com*", "*clarity.ms*",
    "*cdn.cookielaw.org*", "*bombora*", "*6sense*", "*qualified.com*",
    "*munchkin.marketo*", "*bizible*", "*pendo.io*", "*sentry.io*",
    # --- Crunchbase's own client-event counter ---
    # Identified via diagnostic on an org page: /v4/cb/events/clientapp fires
    # again LATE in the load (right as the page settles) - that second call is
    # what records the profile view against the account's quota. Everything we
    # collect is already in the first document response, so dropping it costs no
    # data. (Do NOT block /v4/cb/billing/* - the page needs those to render.)
    "*/v4/cb/events/*",
]

# Markers that a page is being viewed by a logged-IN user (best-effort; CB
# changes these, so we check several).
_LOGGED_IN_MARKERS = ('"is_logged_in":true', '"isLoggedIn":true', '/logout',
                      'data-cy="account-menu"', 'Sign out', 'My Profile')

# If any of these appear, the login FORM is on screen => we are NOT signed in.
# Crunchbase redirects an already-authenticated user away from /login, so the
# absence of this form on /login is a reliable "already logged in" signal.
_LOGIN_FORM_MARKERS = ('type="password"', 'name="password"', 'Forgot password',
                       'Log In With Single Sign-on', 'Send Me a Login Link')

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

    def __init__(self, log=print, proxy=None, email=None, password=None,
                 blocked_urls=None, diagnostic=False, profile_dir=None):
        self.browser = None
        self.log = log
        self.proxy = proxy or None        # 'host:port' or 'http://user:pass@host:port'
        self.profile_dir = profile_dir or PROFILE_DIR   # own Chrome profile
        self._block_streak = 0
        # phase 2
        self.email = email or None
        self.password = password or None
        self.blocked_urls = (list(DEFAULT_BLOCKED_URLS)
                             if blocked_urls is None else list(blocked_urls))
        self.diagnostic = diagnostic
        self.logged_in = False
        self._req_log = []                # request URLs seen during last nav
        self._net_installed = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self):
        if self.browser is not None:
            return
        self.log("Launching browser...")
        os.makedirs(self.profile_dir, exist_ok=True)

        args = []
        server, user, pw = _parse_proxy(self.proxy)
        if server:
            args.append(f"--proxy-server={server}")
            self.log(f"Routing through proxy: {server}")

        self.browser = await uc.start(
            headless=HEADLESS,
            user_data_dir=self.profile_dir,  # warm, persistent profile builds trust
            browser_args=args,
        )
        if server and user:
            await self._install_proxy_auth(user, pw)
        await self._install_network_controls(capture=True)
        if self.blocked_urls:
            self.log(f"Blocking {len(self.blocked_urls)} telemetry/counter "
                     f"URL pattern(s).")
        self.log("Browser ready.")

    async def _install_network_controls(self, tab=None, capture=False):
        """
        On the given tab (default = main tab): enable CDP networking and block
        telemetry + usage-counter URLs so a view is not charged against the
        account quota. Only when `capture` is set (the main / diagnostic tab) do
        we also record every request URL, for beacon-hunting.

        Called once per tab - including each parallel tab - so blocking applies
        everywhere.
        """
        try:
            from nodriver import cdp
        except Exception as e:
            self.log(f"(network controls unavailable: {e})")
            return
        tab = tab or self.browser.main_tab
        try:
            await tab.send(cdp.network.enable())
            if self.blocked_urls:
                # nodriver snake-cases "setBlockedURLs" oddly and the exact name
                # has shifted across versions - resolve it defensively.
                block_fn = (getattr(cdp.network, "set_blocked_ur_ls", None)
                            or getattr(cdp.network, "set_blocked_urls", None)
                            or getattr(cdp.network, "set_blocked_ur_l_s", None))
                if block_fn:
                    await tab.send(block_fn(urls=self.blocked_urls))
                elif capture:
                    self.log("(setBlockedURLs not found in this nodriver build; "
                             "telemetry not blocked)")

            if capture:
                def _on_req(ev):
                    try:
                        self._req_log.append(ev.request.url)
                    except Exception:
                        pass
                tab.add_handler(cdp.network.RequestWillBeSent, _on_req)
            self._net_installed = True
        except Exception as e:
            self.log(f"(could not install network controls: {e})")

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

    # --- login (phase 2) ---------------------------------------------------

    @staticmethod
    def _login_form_present(html):
        """True if the Crunchbase login form is on the page (=> not signed in)."""
        return bool(html) and any(m in html for m in _LOGIN_FORM_MARKERS)

    @staticmethod
    def _looks_logged_in(html, email=None):
        if not html:
            return False
        if CrunchbaseScraper._login_form_present(html):
            return False
        # the strongest signal: the account's own email is embedded in the page
        if email and email.lower() in html.lower():
            return True
        return any(m in html for m in _LOGGED_IN_MARKERS)

    async def _press_enter(self, tab):
        """Dispatch an Enter keypress to whatever element currently has focus."""
        from nodriver import cdp
        for ev in ("keyDown", "keyUp"):
            await tab.send(cdp.input_.dispatch_key_event(
                ev, key="Enter", code="Enter",
                windows_virtual_key_code=13, native_virtual_key_code=13))

    async def ensure_logged_in(self, email=None, password=None):
        """
        Make sure the browser session is authenticated - FULLY automatic when
        credentials are supplied (no human click needed).

        Order:
          1. Reuse the warm profile if it is already signed in.
          2. Otherwise open /login, type the email + password, and submit by
             pressing Enter in the password field. A "Sign In" button click is
             kept only as a fallback if Enter doesn't take.
          3. Only when no credentials are given (or scripted login can't be
             confirmed) do we leave the Chrome window for a manual sign-in;
             the session then persists for next time.

        Returns True if we believe the session is authenticated.
        """
        email = email or self.email
        password = password or self.password
        await self.start()

        # 1) fast path: the home page already shows our account (no /login hit)
        try:
            html = await self.fetch(CB_BASE, expect=None)
            if email and email.lower() in (html or "").lower():
                self.logged_in = True
                self.log("Already logged in (account detected on home page).")
                return True
        except Exception as e:
            self.log(f"  (login pre-check failed: {e})")

        # 2) authoritative: open /login. Crunchbase bounces an authenticated
        #    user off it, so if the login form isn't shown we are already in.
        try:
            html = await self.fetch(CB_LOGIN_URL, expect=None)
        except Exception as e:
            self.log(f"  could not open login page: {e}")
            html = ""
        if html and not self._login_form_present(html):
            self.logged_in = True
            self.log("Already logged in (login page redirected away).")
            return True

        # 3) need to log in but no credentials -> manual sign-in
        if not (email and password):
            self.log("Not logged in - sign in manually in the Chrome window "
                     "(the session will persist for next time).")
            return False

        # 4) scripted auto-login. Clear each field FIRST so a pre-filled value
        #    can't get doubled up (e.g. 'foo@x.comfoo@x.com').
        self.log(f"Auto-logging in as {email} ...")
        tab = self.browser.main_tab
        try:
            await asyncio.sleep(1.0)
            email_el = await tab.select("input[name=email], input[type=email]")
            await email_el.clear_input()
            await email_el.send_keys(email)
            await asyncio.sleep(0.4)
            pass_el = await tab.select("input[name=password], input[type=password]")
            await pass_el.clear_input()
            await pass_el.send_keys(password)
            await asyncio.sleep(0.4)

            # submit by pressing Enter in the password field
            await self._press_enter(tab)
            self.log("  submitted login (Enter); waiting for session...")
            await asyncio.sleep(6.0)

            # fallback: if the form is still up, click the submit button
            html = await tab.get_content()
            if self._login_form_present(html):
                try:
                    btn = await tab.select("button[type=submit]")
                    await btn.click()
                    self.log("  Enter didn't take; clicked Log In, waiting...")
                    await asyncio.sleep(6.0)
                except Exception:
                    pass

            # confirm: reload /login; if the form is gone, we're in
            try:
                html = await self.fetch(CB_LOGIN_URL, expect=None)
                self.logged_in = not self._login_form_present(html)
            except Exception:
                self.logged_in = self._looks_logged_in(
                    await tab.get_content(), email)
        except Exception as e:
            self.log(f"  scripted login failed ({e}); finish it manually "
                     f"in the Chrome window.")
            self.logged_in = False

        self.log("Login confirmed - session ready." if self.logged_in
                 else "Login NOT confirmed - complete it in the browser window, "
                      "then start the run again.")
        return self.logged_in

    def _dump_diagnostics(self, slug, html):
        """Write request log + funding ng-state keys for one org to data/."""
        try:
            import json
            out_dir = os.path.join(_app_dir(), "data")
            os.makedirs(out_dir, exist_ok=True)
            safe = re.sub(r"[^a-z0-9_-]", "_", (slug or "page").lower())[:60]
            payload = {
                "slug": slug,
                "logged_in": self.logged_in,
                "requests_during_load": list(self._req_log),
                "funding_state": cb_parser.funding_debug(html),
            }
            path = os.path.join(out_dir, f"diag_{safe}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
            self.log(f"  [diagnostic] wrote {path} "
                     f"({len(self._req_log)} requests captured)")
        except Exception as e:
            self.log(f"  [diagnostic] dump failed: {e}")

    # --- low-level fetch ---------------------------------------------------

    async def _navigate(self, url, expect, tab=None):
        """One navigation attempt. Returns HTML, or raises (block / conn loss).

        With `tab` given, navigates that tab (parallel mode). Otherwise it uses
        the main tab via browser.get and records requests for diagnostics.
        """
        if tab is None:
            self._req_log = []           # fresh request capture for this nav
            tab = await asyncio.wait_for(self.browser.get(url), timeout=NAV_TIMEOUT)
        else:
            await asyncio.wait_for(tab.get(url), timeout=NAV_TIMEOUT)

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

    async def fetch(self, url, expect="ng-state", tab=None):
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
                return await self._navigate(url, expect, tab=tab)
            except RuntimeError:
                raise                       # genuine Cloudflare block
            except Exception as e:
                # only the main-tab path may relaunch the whole browser; a
                # provided tab (parallel) just fails this one domain.
                if tab is None and _is_conn_error(e) and attempt == 1:
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

    async def _search_engine_slug(self, domain, tab=None):
        """Ask Google (then Bing) which Crunchbase org page matches a domain.

        Google resolves the right org page more often, so we try it first and
        only fall back to Bing when Google returns nothing - usually saving the
        second search entirely.
        """
        query = f'"{domain}" crunchbase'
        engines = [
            ("Google", f"https://www.google.com/search?q={quote_plus(query)}"),
            ("Bing", f"https://www.bing.com/search?q={quote_plus(query)}"),
        ]
        for name, url in engines:
            try:
                self.log(f"  searching {name} for {domain}")
                html = await self.fetch(url, expect=None, tab=tab)
                links = self._crunchbase_links(html)
                if links:
                    self.log(f"  {name} -> {links[0]}")
                    return links[0]
            except Exception as e:
                self.log(f"  {name} search failed: {e}")
            await self._polite_pause()
        return None

    @staticmethod
    def _domain_matches(input_domain, website):
        """
        True / False if the org's listed website matches the input domain;
        None when Crunchbase lists no website (so it can't be verified).
        Crunchbase shows the website on every org page even without login, so
        this catches search results that resolved to the WRONG company.
        """
        site = cb_parser.domain_of(website)
        d = (input_domain or "").lower()
        if not site:
            return None
        return site == d or site.endswith("." + d) or d.endswith("." + site)

    async def resolve(self, domain, tab=None):
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
                html = await self.fetch(url, tab=tab)
            except Exception as e:
                self.log(f"  guess '{slug}' failed: {e}")
                continue
            rec = cb_parser.parse_organization(html)
            if not rec:
                self.log(f"  guess '{slug}': no Crunchbase page there")
                continue
            if self._domain_matches(domain, rec.get("website")):
                self.log(f"  matched by direct guess: {slug}")
                return rec.get("crunchbase_permalink") or slug, html
            site = cb_parser.domain_of(rec.get("website"))
            self.log(f"  guess '{slug}' is a different company ({site or '?'})")

        # 2) search engines
        slug = await self._search_engine_slug(domain, tab=tab)
        if not slug:
            return None, None

        # load the searched org page; one reload retry if parsing fails
        for attempt in (1, 2):
            try:
                html = await self.fetch(
                    f"https://www.crunchbase.com/organization/{slug}", tab=tab)
            except Exception as e:
                self.log(f"  loading '{slug}' failed: {e}")
                return None, None
            rec = cb_parser.parse_organization(html)
            if rec:
                # NB: the website check happens in scrape_domain so the matched
                # record is still surfaced (flagged) rather than silently lost.
                return rec.get("crunchbase_permalink") or slug, html
            self.log(f"  '{slug}' loaded but org data not parsed "
                     f"(html={len(html)}, ng-state={'ng-state' in html}, "
                     f"attempt {attempt})")
            await self._polite_pause()
        return None, None

    # --- person enrichment -------------------------------------------------

    async def enrich_people(self, people, max_people, tab=None):
        """Visit individual person pages to pull socials / website / email."""
        for person in people[:max_people]:
            if not person.get("crunchbase_url"):
                continue
            try:
                await self._polite_pause()
                html = await self.fetch(person["crunchbase_url"], tab=tab)
                extra = cb_parser.parse_person(html)
                for k, v in extra.items():
                    person[k] = v
                self.log(f"    + person: {person['name']}")
            except Exception as e:
                self.log(f"    person '{person['name']}' failed: {e}")

    # --- the public per-domain entry point --------------------------------

    async def scrape_domain(self, domain, enrich=False, max_people=5, tab=None):
        """
        Full pipeline for one domain. Returns a result dict with keys:
        status ('done' | 'mismatch' | 'not_found' | 'error'), record, error.

        'mismatch' means a Crunchbase company was found but its listed website
        does not match the requested domain (likely a wrong search hit) - the
        record is still returned so it can be reviewed, just clearly flagged.

        When `tab` is given the work runs in that tab (parallel mode); the
        browser-restart maintenance is then skipped (it would kill sibling tabs).
        """
        if tab is None:
            await self._maybe_maintenance()
        try:
            permalink, html = await self.resolve(domain, tab=tab)
            if not permalink or not html:
                return {"status": "not_found", "record": None,
                        "error": "No matching Crunchbase organization found"}

            record = cb_parser.parse_organization(html)
            if not record:
                return {"status": "error", "record": None,
                        "error": "Found page but could not parse data"}

            if self.diagnostic and tab is None:
                self._dump_diagnostics(record.get("crunchbase_permalink")
                                       or permalink, html)

            record["input_domain"] = domain
            match = self._domain_matches(domain, record.get("website"))
            record["matched_website"] = cb_parser.domain_of(record.get("website"))
            record["domain_match"] = match

            if match is False:
                # found a company, but it's the wrong one for this domain
                return {"status": "mismatch", "record": record,
                        "error": (f"Found '{record.get('name')}' "
                                  f"({record['matched_website'] or '?'}) - website "
                                  f"does not match input domain '{domain}'")}

            if enrich and record.get("key_people"):
                await self.enrich_people(record["key_people"], max_people, tab=tab)

            return {"status": "done", "record": record, "error": None}
        except Exception as e:
            return {"status": "error", "record": None, "error": str(e)}
        finally:
            await self._polite_pause()
