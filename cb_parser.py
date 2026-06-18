"""
cb_parser.py
------------
Pure parsing logic: turn a Crunchbase organization (or person) HTML page into a
clean Python dict.

Crunchbase is an Angular app. Every page server-side-renders a giant JSON blob
into  <script id="ng-state">...</script>. Inside it, `HttpState` caches the raw
API responses the page used. We never touch the visible HTML - we read that
cache. It contains more fields than the page shows and never breaks on CSS
changes.
"""

import json
import re
from urllib.parse import urlparse

# --- enum lookup tables -----------------------------------------------------

EMPLOYEE_ENUM = {
    "c_00001_00010": "1-10",
    "c_00011_00050": "11-50",
    "c_00051_00100": "51-100",
    "c_00101_00250": "101-250",
    "c_00251_00500": "251-500",
    "c_00501_01000": "501-1000",
    "c_01001_05000": "1001-5000",
    "c_05001_10000": "5001-10000",
    "c_10001_max": "10001+",
}

FUNDING_TYPE = {
    "pre_seed": "Pre-Seed",
    "seed": "Seed",
    "angel": "Angel",
    "series_a": "Series A",
    "series_b": "Series B",
    "series_c": "Series C",
    "series_d": "Series D",
    "series_e": "Series E",
    "series_f": "Series F",
    "series_g": "Series G",
    "series_h": "Series H",
    "series_unknown": "Venture - Series Unknown",
    "private_equity": "Private Equity",
    "debt_financing": "Debt Financing",
    "grant": "Grant",
    "convertible_note": "Convertible Note",
    "non_equity_assistance": "Non-equity Assistance",
    "equity_crowdfunding": "Equity Crowdfunding",
    "product_crowdfunding": "Product Crowdfunding",
    "post_ipo_equity": "Post-IPO Equity",
    "post_ipo_debt": "Post-IPO Debt",
    "corporate_round": "Corporate Round",
    "initial_coin_offering": "Initial Coin Offering",
    "secondary_market": "Secondary Market",
    "undisclosed": "Undisclosed",
}


# --- low level --------------------------------------------------------------

def extract_ng_state(html):
    """Return the parsed `ng-state` JSON dict, or None if not present."""
    # attribute-order-independent: id may not be the first attribute
    m = re.search(r'<script\b[^>]*\bid="ng-state"[^>]*>(.*?)</script>',
                  html or "", re.S | re.I)
    if not m:
        # last resort: any <script> blob that carries the state cache
        for blob in re.findall(r'<script\b[^>]*>(.*?)</script>', html or "",
                               re.S | re.I):
            if '"HttpState"' in blob or '"apollo.state"' in blob:
                m = type("M", (), {"group": lambda self, i: blob})()
                break
    if not m:
        return None
    raw = m.group(1)
    # Angular escapes &q; &a; etc. in older builds; modern builds emit plain
    # JSON. Try plain first, then the legacy unescape.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    raw2 = (raw.replace("&q;", '"').replace("&a;", "&")
               .replace("&s;", "'").replace("&l;", "<").replace("&g;", ">"))
    try:
        return json.loads(raw2)
    except json.JSONDecodeError:
        pass
    # last attempt: standard HTML entities
    raw3 = (raw.replace("&quot;", '"').replace("&#34;", '"')
               .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    try:
        return json.loads(raw3)
    except json.JSONDecodeError:
        return None


def _entity_from_state(ng_state, path_fragment):
    """Pull the cached API entity body whose request path contains `fragment`."""
    if not ng_state:
        return None
    http_state = ng_state.get("HttpState", {})
    for key, val in http_state.items():
        if path_fragment in key:
            data = val.get("data") if isinstance(val, dict) else None
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    continue
            if isinstance(data, dict) and ("cards" in data or "properties" in data):
                return data
    return None


def get_org_entity(html):
    """Parsed organization entity {properties, cards} from an org page HTML."""
    return _entity_from_state(extract_ng_state(html),
                              "/entities/organizations/")


def get_person_entity(html):
    """Parsed person entity {properties, cards} from a person page HTML."""
    return _entity_from_state(extract_ng_state(html), "/entities/people/")


# --- helpers ----------------------------------------------------------------

def _val(node):
    """Crunchbase wraps many scalars as {'value': X}. Unwrap defensively."""
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def _locations(identifiers):
    """Order location identifiers as City, Region, Country."""
    if not identifiers:
        return {}
    by_type = {}
    for loc in identifiers:
        by_type.setdefault(loc.get("location_type"), loc.get("value"))
    return by_type


def domain_of(url):
    """Bare registrable host of a URL, lowercased, no www/path."""
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _money(node):
    """
    Format a Crunchbase money field as a human string, or None if absent.
    CB money looks like {'value': 1000000, 'value_usd': 1000000, 'currency': 'USD'}.
    Logged-out pages omit these entirely (that's the "Locked" case).
    """
    # NB: do NOT _val() this - CB money dicts carry a "value" key, so _val would
    # strip the currency and silently mislabel everything as USD.
    if node in (None, "", [], {}):
        return None
    if isinstance(node, dict):
        usd = node.get("value_usd")
        if usd is not None:                       # USD-normalised amount
            amt, cur = usd, "USD"
        else:
            amt, cur = node.get("value"), node.get("currency") or ""
        if amt is None:
            return None
        try:
            amt_s = f"{float(amt):,.0f}"
        except (TypeError, ValueError):
            return str(amt)
        return f"${amt_s}" if cur in ("USD", "") else f"{cur} {amt_s}"
    if isinstance(node, (int, float)):
        return f"${node:,.0f}"
    return str(node)


def funding_debug(html):
    """
    Diagnostic helper: return all card keys plus every funding/financials card
    raw, so we can see exactly what a LOGGED-IN page exposes that an anonymous
    one does not. Run once with a logged-in session, inspect the dump.
    """
    entity = get_org_entity(html) or {}
    cards = entity.get("cards", {}) or {}
    props = entity.get("properties", {}) or {}
    out = {"all_card_keys": sorted(cards.keys()),
           "property_keys": sorted(props.keys())}
    for k, v in cards.items():
        kl = k.lower()
        if "fund" in kl or "financ" in kl or "invest" in kl:
            out[f"card::{k}"] = v
    return out


# --- organization parsing ---------------------------------------------------

def parse_organization(html):
    """
    Turn an organization page HTML into the structured record we want.
    Returns None if the page contained no organization entity.
    """
    entity = get_org_entity(html)
    if not entity:
        return None

    props = entity.get("properties", {}) or {}
    cards = entity.get("cards", {}) or {}

    about = cards.get("company_about_fields2", {}) or {}
    extended = cards.get("overview_fields_extended", {}) or {}
    gh = cards.get("growth_and_heat", {}) or {}
    social = cards.get("social_fields", {}) or {}
    contact = cards.get("contact_fields", {}) or {}
    financials = cards.get("company_financials_highlights", {}) or {}
    funding_sum = cards.get("funding_rounds_summary", {}) or {}

    locs = _locations(about.get("location_identifiers"))

    rec = {}

    # --- identity -----------------------------------------------------------
    rec["name"] = props.get("title") or _val(funding_sum.get("identifier"))
    rec["crunchbase_permalink"] = (props.get("identifier") or {}).get("permalink")
    rec["crunchbase_url"] = (
        f"https://www.crunchbase.com/organization/{rec['crunchbase_permalink']}"
        if rec.get("crunchbase_permalink") else None
    )

    # --- scores (the headline numbers) -------------------------------------
    rec["growth_score"] = gh.get("growth_score")
    rec["growth_score_delta_90d"] = gh.get("growth_score_delta_d90")
    rec["heat_score"] = gh.get("heat_score")
    rec["heat_score_delta_90d"] = gh.get("heat_score_delta_d90")
    rec["cb_rank"] = about.get("rank_org_company")
    rec["cb_rank_delta_90d"] = props.get("rank_delta_d90")

    # --- description --------------------------------------------------------
    # short_description is the one-liner CB shows under the name.
    rec["description"] = props.get("short_description")
    rec["description_long"] = (cards.get("overview_description") or {}).get("description")

    # --- company facts ------------------------------------------------------
    ipo = about.get("ipo_status")
    rec["company_status"] = {"private": "Private", "public": "Public",
                             "delisted": "Delisted"}.get(ipo, ipo)
    rec["operating_status"] = extended.get("operating_status")
    rec["funding_stage"] = FUNDING_TYPE.get(about.get("last_funding_type"),
                                            about.get("last_funding_type"))
    rec["employee_count"] = EMPLOYEE_ENUM.get(about.get("num_employees_enum"),
                                              about.get("num_employees_enum"))
    rec["legal_name"] = extended.get("legal_name")
    rec["website"] = _val(about.get("website"))

    rec["city"] = locs.get("city")
    rec["region"] = locs.get("region")
    rec["country"] = locs.get("country")
    rec["location"] = ", ".join(
        x for x in (locs.get("city"), locs.get("region"), locs.get("country")) if x
    )

    # --- acquisition --------------------------------------------------------
    # Only `acquirer_identifier` proves an acquisition. The bare `identifier`
    # in acquired_by_summary is the company ITSELF - never use it here.
    rec["acquired"] = False
    rec["acquired_by"] = None
    rec["acquisition_name"] = None
    for ckey in ("acquired_by_fields", "acquired_by_summary"):
        acq = cards.get(ckey)
        if isinstance(acq, dict) and acq.get("acquirer_identifier"):
            rec["acquired"] = True
            rec["acquired_by"] = _val(acq["acquirer_identifier"])
            deal = acq.get("acquisition_identifier")
            rec["acquisition_name"] = _val(deal) if deal else None
            break

    # --- funding ------------------------------------------------------------
    # Funding TOTAL is omitted from the page for logged-out visitors. With a
    # logged-in session the real amount IS present in funding_rounds_summary /
    # financials. We return the raw value or None here; the SCRAPER fills the
    # fallback text, because only it knows whether we are logged in:
    #   logged out         -> "Locked (login required)"
    #   logged in, absent  -> blank (the company simply has no total)
    # That avoids the misleading "login required" on a logged-in run.
    rec["funding_total"] = (
        _money(funding_sum.get("funding_total"))
        or _money(financials.get("funding_total"))
        or _money(funding_sum.get("funding_total_usd"))
    )
    rec["last_funding_amount"] = (
        _money(funding_sum.get("last_funding_total"))
        or _money(financials.get("last_funding_total"))
    )
    rec["last_funding_date"] = (funding_sum.get("last_funding_at")
                                or about.get("last_funding_at"))
    rec["num_funding_rounds"] = (financials.get("num_funding_rounds")
                                 or funding_sum.get("num_funding_rounds"))
    rec["num_investors"] = financials.get("num_investors")
    rec["num_investments_made"] = financials.get("num_investments")

    # --- news ---------------------------------------------------------------
    timeline = cards.get("overview_timeline") or {}
    news_count = timeline.get("count")
    if news_count is None:
        entities = timeline.get("entities")
        news_count = len(entities) if isinstance(entities, list) else 0
    rec["news_available"] = "yes" if (news_count or 0) > 0 else "no"
    rec["news_count"] = news_count or 0

    # --- contact / social ---------------------------------------------------
    rec["contact_email"] = contact.get("contact_email")
    rec["phone"] = contact.get("phone_number")
    rec["facebook"] = _val(social.get("facebook"))
    rec["linkedin"] = _val(social.get("linkedin"))
    rec["twitter"] = _val(social.get("twitter"))

    sem = cards.get("semrush_rank_headline") or {}
    rec["monthly_web_visits"] = sem.get("semrush_visits_latest_month")

    # --- key people ---------------------------------------------------------
    rec["key_people"] = _parse_key_people(cards)

    return rec


def _parse_key_people(cards):
    """Collect leadership/employees listed on the org page."""
    people = []
    seen = set()
    # Featured employees first (the ones CB shows on the overview tab),
    # then the wider current-employee list, then advisors.
    for list_key in ("current_employees_featured_order_field",
                      "current_employees_image_list",
                      "current_advisors_image_list"):
        items = cards.get(list_key)
        if not isinstance(items, list):
            continue
        for item in items:
            pid = item.get("person_identifier") or {}
            permalink = pid.get("permalink")
            name = pid.get("value")
            if not name or permalink in seen:
                continue
            seen.add(permalink)
            people.append({
                "name": name,
                "title": item.get("title"),
                "permalink": permalink,
                "crunchbase_url": (
                    f"https://www.crunchbase.com/person/{permalink}"
                    if permalink else None
                ),
                # filled in later if person-page enrichment is enabled
                "linkedin": None, "twitter": None, "facebook": None,
                "website": None, "email": None,
            })
    return people


# --- person parsing (enrichment) -------------------------------------------

_URL_FIELDS = ("linkedin", "twitter", "facebook", "website")


def parse_person(html):
    """
    Extract socials/website/email from a person page HTML.
    Returns a dict of whatever was found (keys: linkedin, twitter, facebook,
    website, email, current_title, current_organization).
    """
    entity = get_person_entity(html)
    if not entity:
        return {}

    cards = entity.get("cards", {}) or {}
    out = {}

    # Socials & website live in overview_fields2 / overview_fields_v2.
    for ckey in ("overview_fields2", "overview_fields_v2", "overview_fields"):
        card = cards.get(ckey) or {}
        for field in _URL_FIELDS:
            if not out.get(field) and field in card:
                out[field] = _val(card.get(field))

    # Email, if exposed publicly.
    contact = cards.get("contact_fields") or cards.get("overview_fields") or {}
    if isinstance(contact, dict):
        out["email"] = contact.get("contact_email") or out.get("email")

    # Current job / organization.
    jobs = cards.get("current_jobs_image_list")
    if isinstance(jobs, list) and jobs:
        job = jobs[0]
        out["current_title"] = job.get("title")
        org = job.get("organization_identifier") or job.get("identifier")
        out["current_organization"] = _val(org) if org else None

    return {k: v for k, v in out.items() if v}
