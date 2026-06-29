"""
app.py
------
The single interface: a small local web app.

  * paste / upload a list of domains
  * hit Start - a background worker drives one Chrome through Crunchbase
  * watch live progress + a running log
  * export results as CSV (companies + a separate people CSV) or JSON

Run:   python app.py      then open  http://127.0.0.1:5000
"""

import csv
import io
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import nodriver as uc
from cb_scraper import CrunchbaseScraper


def _resource_dir():
    """Where bundled resources (templates) live - the PyInstaller extraction
    dir when frozen, otherwise this file's folder."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


app = Flask(__name__, template_folder=os.path.join(_resource_dir(), "templates"))
# pick up template edits without a server restart (dev convenience; harmless
# in the packaged app where templates don't change)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# --------------------------------------------------------------------------
# shared run state - mutated only by the worker thread, read by Flask
# --------------------------------------------------------------------------
LOCK = threading.Lock()
STATE = {
    "running": False,
    "stop_requested": False,
    "started_at": None,
    "finished_at": None,
    "options": {},
    "rows": [],       # [{domain, status, record, error}]
    "log": [],        # recent log lines
}


def _log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    with LOCK:
        STATE["log"].append(line)
        del STATE["log"][:-300]   # keep last 300 lines
    print(line, flush=True)


# --------------------------------------------------------------------------
# background worker: its own thread + asyncio loop, owns the browser
# --------------------------------------------------------------------------
class Worker:
    def __init__(self):
        self.loop = None
        self.scraper = None
        self._ready = threading.Event()
        threading.Thread(target=self._thread_main, daemon=True).start()
        self._ready.wait()

    def _thread_main(self):
        self.loop = uc.loop()                 # the loop nodriver expects
        self.scraper = CrunchbaseScraper(log=_log)
        self._ready.set()
        self.loop.run_forever()

    def submit(self, domains, options):
        """Kick off a run. Returns False if one is already in progress."""
        with LOCK:
            if STATE["running"]:
                return False
            STATE.update(
                running=True, stop_requested=False,
                started_at=datetime.now().isoformat(), finished_at=None,
                options=options,
                rows=[{"domain": d, "status": "queued",
                       "record": None, "error": None} for d in domains],
                log=[],
            )
        _log(f"Run started: {len(domains)} domain(s). "
             f"People enrichment: {'ON' if options['enrich'] else 'OFF'}.")
        import asyncio
        asyncio.run_coroutine_threadsafe(self._run(options), self.loop)
        return True

    def login(self, options):
        """Open the browser and (try to) authenticate ahead of a run."""
        import asyncio
        asyncio.run_coroutine_threadsafe(self._login(options), self.loop)
        return True

    async def _login(self, options):
        try:
            if (options.get("proxy") or None) != self.scraper.proxy:
                self.scraper.proxy = options.get("proxy") or None
                await self.scraper.stop()
            self.scraper.email = options.get("email") or None
            self.scraper.password = options.get("password") or None
            await self.scraper.ensure_logged_in()
        except Exception as e:
            _log(f"Login error: {e}")

    async def _run(self, options):
        try:
            # apply a proxy change by relaunching the browser
            if (options.get("proxy") or None) != self.scraper.proxy:
                self.scraper.proxy = options.get("proxy") or None
                await self.scraper.stop()
            # phase-2 options
            self.scraper.email = options.get("email") or None
            self.scraper.password = options.get("password") or None
            self.scraper.diagnostic = bool(options.get("diagnostic"))
            await self.scraper.start()
            if options.get("login"):
                await self.scraper.ensure_logged_in()

            # diagnostic mode records per-request data on the main tab, so it
            # only makes sense single-tab.
            conc = int(options.get("concurrency") or 1)
            if options.get("diagnostic"):
                conc = 1
            if conc > 1:
                await self._run_parallel(options, conc)
            else:
                await self._run_sequential(options)
        except Exception as e:
            _log(f"FATAL: {e}")
        finally:
            with LOCK:
                STATE["running"] = False
                STATE["finished_at"] = datetime.now().isoformat()
            done = sum(1 for r in STATE["rows"] if r["status"] == "done")
            _log(f"Run finished. {done}/{len(STATE['rows'])} succeeded.")

    @staticmethod
    def _finish_row(idx, result):
        """Write a finished scrape result into STATE and log a one-liner."""
        with LOCK:
            STATE["rows"][idx].update(
                status=result["status"], record=result["record"],
                error=result["error"],
            )
        if result["status"] == "done":
            rec = result["record"]
            _log(f"  OK - {rec.get('name')} "
                 f"(growth={rec.get('growth_score')}, rank={rec.get('cb_rank')})")
        elif result["status"] == "mismatch":
            _log(f"  MISMATCH - {result['error']}")
        else:
            _log(f"  {result['status'].upper()} - {result['error']}")

    async def _run_sequential(self, options):
        total = len(STATE["rows"])
        for idx, row in enumerate(list(STATE["rows"])):
            with LOCK:
                if STATE["stop_requested"]:
                    _log("Stop requested - halting run.")
                    break
                STATE["rows"][idx]["status"] = "running"
            _log(f"({idx + 1}/{total}) {row['domain']}")
            result = await self.scraper.scrape_domain(
                row["domain"], enrich=options["enrich"],
                max_people=options["max_people"],
            )
            self._finish_row(idx, result)

    async def _run_parallel(self, options, conc):
        """Run `conc` independent browsers in parallel, each pulling domains
        from a shared queue. nodriver serializes work within one browser, so
        true parallelism needs separate browser instances (each its own Chrome
        profile + login session)."""
        import asyncio
        from cb_scraper import CrunchbaseScraper, PROFILE_DIR

        total = len(STATE["rows"])

        # worker 0 is the main scraper (already started + logged in); spin up
        # the rest, each with its own persistent profile.
        pool = [self.scraper]
        for i in range(1, conc):
            pool.append(CrunchbaseScraper(
                log=_log,
                proxy=options.get("proxy") or None,
                email=options.get("email") or None,
                password=options.get("password") or None,
                profile_dir=f"{PROFILE_DIR}_{i}",
            ))

        _log(f"Parallel mode: {conc} browsers. Warming up extra browser(s)...")

        async def prep(s):
            try:
                await s.start()
                if options.get("login"):
                    await s.ensure_logged_in()
            except Exception as e:
                _log(f"  worker browser prep failed: {e}")

        await asyncio.gather(*(prep(s) for s in pool[1:]))

        queue = asyncio.Queue()
        for idx, row in enumerate(list(STATE["rows"])):
            queue.put_nowait((idx, row["domain"]))

        async def runner(s, wid):
            while True:
                try:
                    idx, domain = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                with LOCK:
                    if STATE["stop_requested"]:
                        return
                    STATE["rows"][idx]["status"] = "running"
                _log(f"[w{wid}] ({idx + 1}/{total}) {domain}")
                try:
                    result = await s.scrape_domain(
                        domain, enrich=options["enrich"],
                        max_people=options["max_people"],
                    )
                except Exception as e:
                    result = {"status": "error", "record": None, "error": str(e)}
                self._finish_row(idx, result)

        try:
            await asyncio.gather(*(runner(s, i) for i, s in enumerate(pool)))
        finally:
            # tear down the extra browsers; keep the main one warm for next run
            for s in pool[1:]:
                try:
                    await s.stop()
                except Exception:
                    pass


WORKER = Worker()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def parse_domains(text):
    """Accept newline / comma / space separated domains; clean each one."""
    import re
    raw = re.split(r"[\s,;]+", text or "")
    seen, out = set(), []
    for token in raw:
        token = token.strip().lower()
        if not token:
            continue
        token = re.sub(r"^https?://", "", token).split("/")[0]
        if token and token not in seen and "." in token:
            seen.add(token)
            out.append(token)
    return out


COMPANY_COLUMNS = [
    "input_domain", "name", "crunchbase_url", "growth_score",
    "growth_score_delta_90d", "cb_rank", "cb_rank_delta_90d", "heat_score",
    "heat_score_delta_90d", "description", "company_status",
    "operating_status", "funding_stage", "employee_count", "location",
    "city", "region", "country", "website", "domain_match", "matched_website",
    "legal_name", "acquired",
    "acquired_by", "funding_total", "last_funding_amount", "last_funding_date",
    "num_funding_rounds", "num_investors",
    "news_available", "news_count", "contact_email", "phone", "linkedin",
    "twitter", "facebook", "monthly_web_visits", "key_people_count",
    "status", "error",
]


def company_rows():
    """Flatten STATE rows into CSV-ready company dicts."""
    out = []
    with LOCK:
        rows = list(STATE["rows"])
    for row in rows:
        rec = row.get("record") or {}
        flat = {c: rec.get(c) for c in COMPANY_COLUMNS}
        flat["input_domain"] = rec.get("input_domain") or row["domain"]
        flat["key_people_count"] = len(rec.get("key_people") or [])
        flat["status"] = row["status"]
        flat["error"] = row["error"]
        out.append(flat)
    return out


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    text = request.form.get("domains", "")
    if request.files.get("file"):
        text += "\n" + request.files["file"].read().decode("utf-8", "ignore")
    domains = parse_domains(text)
    if not domains:
        return jsonify(ok=False, error="No valid domains found."), 400

    try:
        concurrency = int(request.form.get("concurrency") or 1)
    except ValueError:
        concurrency = 1
    concurrency = max(1, min(6, concurrency))   # 1..6 tabs

    options = {
        "enrich": request.form.get("enrich") == "on",
        "max_people": int(request.form.get("max_people") or 5),
        "proxy": (request.form.get("proxy") or "").strip(),
        "login": request.form.get("login") == "on",
        "diagnostic": request.form.get("diagnostic") == "on",
        "email": (request.form.get("email") or "").strip(),
        "password": request.form.get("password") or "",
        "concurrency": concurrency,
    }
    if not WORKER.submit(domains, options):
        return jsonify(ok=False, error="A run is already in progress."), 409
    return jsonify(ok=True, count=len(domains))


@app.route("/login", methods=["POST"])
def login():
    """Open Chrome and authenticate before a run (so the session is warm)."""
    with LOCK:
        if STATE["running"]:
            return jsonify(ok=False, error="A run is in progress."), 409
    options = {
        "proxy": (request.form.get("proxy") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "password": request.form.get("password") or "",
    }
    WORKER.login(options)
    return jsonify(ok=True)


@app.route("/stop", methods=["POST"])
def stop():
    with LOCK:
        STATE["stop_requested"] = True
    return jsonify(ok=True)


@app.route("/status")
def status():
    with LOCK:
        rows = [{"domain": r["domain"], "status": r["status"],
                 "error": r["error"], "record": r["record"]}
                for r in STATE["rows"]]
        payload = {
            "running": STATE["running"],
            "started_at": STATE["started_at"],
            "finished_at": STATE["finished_at"],
            "log": STATE["log"][-120:],
            "rows": rows,
        }
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    payload["counts"] = counts
    payload["total"] = len(rows)
    return jsonify(payload)


@app.route("/export.json")
def export_json():
    with LOCK:
        records = [r["record"] for r in STATE["rows"] if r["record"]]
    return Response(
        json.dumps(records, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=crunchbase.json"},
    )


@app.route("/export.csv")
def export_csv():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COMPANY_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in company_rows():
        writer.writerow(row)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=crunchbase_companies.csv"},
    )


@app.route("/export_people.csv")
def export_people_csv():
    cols = ["company", "company_domain", "name", "title", "crunchbase_url",
            "linkedin", "twitter", "facebook", "website", "email",
            "current_title", "current_organization"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    with LOCK:
        rows = list(STATE["rows"])
    for row in rows:
        rec = row.get("record") or {}
        for person in rec.get("key_people") or []:
            entry = dict(person)
            entry["company"] = rec.get("name")
            entry["company_domain"] = rec.get("input_domain") or row["domain"]
            writer.writerow(entry)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=crunchbase_people.csv"},
    )


# Port is fixed at 5000 by default, but can be overridden (e.g. if something
# else is squatting on 5000) via the CBS_PORT environment variable.
PORT = int(os.environ.get("CBS_PORT") or 5000)


def _open_browser_when_ready():
    """Open the UI in the default browser once the server is up."""
    import urllib.request
    url = f"http://127.0.0.1:{PORT}/"
    for _ in range(40):
        time.sleep(0.4)
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            continue
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    print("\n" + "=" * 56)
    print("  Crunchbase Scraper is running.")
    print(f"  Open this in your browser:  http://127.0.0.1:{PORT}")
    print("  Keep this window open while you work. Close it to stop.")
    print("=" * 56 + "\n")
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    # threaded=True: the default single-threaded dev server lets one browser's
    # keep-alive connection block all other requests (status polling, exports,
    # a second tab) - which manifests as the whole UI hanging. Threading fixes it.
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False,
            threaded=True)
