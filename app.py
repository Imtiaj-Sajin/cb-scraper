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
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import nodriver as uc
from cb_scraper import CrunchbaseScraper

app = Flask(__name__)

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

    async def _run(self, options):
        try:
            # apply a proxy change by relaunching the browser
            if (options.get("proxy") or None) != self.scraper.proxy:
                self.scraper.proxy = options.get("proxy") or None
                await self.scraper.stop()
            await self.scraper.start()
            for idx, row in enumerate(list(STATE["rows"])):
                with LOCK:
                    if STATE["stop_requested"]:
                        _log("Stop requested - halting run.")
                        break
                    STATE["rows"][idx]["status"] = "running"
                domain = row["domain"]
                _log(f"({idx + 1}/{len(STATE['rows'])}) {domain}")

                result = await self.scraper.scrape_domain(
                    domain, enrich=options["enrich"],
                    max_people=options["max_people"],
                )
                with LOCK:
                    STATE["rows"][idx].update(
                        status=result["status"],
                        record=result["record"],
                        error=result["error"],
                    )
                if result["status"] == "done":
                    nm = result["record"].get("name")
                    _log(f"  OK - {nm} "
                         f"(growth={result['record'].get('growth_score')}, "
                         f"rank={result['record'].get('cb_rank')})")
                else:
                    _log(f"  {result['status'].upper()} - {result['error']}")
        except Exception as e:
            _log(f"FATAL: {e}")
        finally:
            with LOCK:
                STATE["running"] = False
                STATE["finished_at"] = datetime.now().isoformat()
            done = sum(1 for r in STATE["rows"] if r["status"] == "done")
            _log(f"Run finished. {done}/{len(STATE['rows'])} succeeded.")


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
    "city", "region", "country", "website", "legal_name", "acquired",
    "acquired_by", "funding_total", "num_funding_rounds", "num_investors",
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

    options = {
        "enrich": request.form.get("enrich") == "on",
        "max_people": int(request.form.get("max_people") or 5),
        "proxy": (request.form.get("proxy") or "").strip(),
    }
    if not WORKER.submit(domains, options):
        return jsonify(ok=False, error="A run is already in progress."), 409
    return jsonify(ok=True, count=len(domains))


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


if __name__ == "__main__":
    print("\n  Crunchbase Scraper  ->  http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
