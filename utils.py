# backend/utils.py
import os
import json
import time
import requests
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from pymongo import MongoClient
import phonenumbers
from phonenumbers import PhoneNumberMatcher, format_number, PhoneNumberFormat
from PyPDF2 import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import io
import tempfile

load_dotenv()

# Env keys
NUMVERIFY_KEY = os.getenv("NUMVERIFY_API_KEY", "").strip()
OPENCAGE_KEY = os.getenv("OPENCAGE_API_KEY", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()

# Endpoints
NUMVERIFY_URL = "http://apilayer.net/api/validate"
OPENCAGE_URL = "https://api.opencagedata.com/geocode/v1/json"

# Cache settings
CACHE_FILE = "cache.json"
CACHE_TTL = 7 * 24 * 3600  # 7 days

# Mongo client (lazy)
_mongo_client = None
def get_db():
    global _mongo_client
    if not _mongo_client:
        if not MONGO_URI:
            raise RuntimeError("MONGO_URI not set in environment")
        _mongo_client = MongoClient(MONGO_URI)
    # default DB from URI: return default database
    return _mongo_client["pravanshuosint"]


##### Simple file cache #####
def _load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cache(d: Dict[str, Any]):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

def cache_get(key: str) -> Optional[Any]:
    d = _load_cache()
    rec = d.get(key)
    if not rec:
        return None
    if time.time() - rec.get("ts", 0) > CACHE_TTL:
        d.pop(key, None)
        _save_cache(d)
        return None
    return rec.get("data")

def cache_set(key: str, data: Any):
    d = _load_cache()
    d[key] = {"ts": time.time(), "data": data}
    _save_cache(d)


##### Provider wrappers #####
def call_numverify(number: str) -> Dict[str, Any]:
    """Call NumVerify (apilayer). Returns provider JSON or error dict."""
    key = f"numverify:{number}"
    cached = cache_get(key)
    if cached:
        return cached
    if not NUMVERIFY_KEY:
        return {"error": "NUMVERIFY_KEY not set"}
    params = {"access_key": NUMVERIFY_KEY, "number": number}
    try:
        r = requests.get(NUMVERIFY_URL, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        data = {"error": str(e)}
    cache_set(key, data)
    return data

def call_fraudscore(number: str) -> Dict[str, Any]:
    """Placeholder for FraudScore integration (not implemented)."""
    return {"note": "fraudscore_not_configured"}

def call_twilio_lookup(number: str) -> Dict[str, Any]:
    """Placeholder for Twilio Lookup integration (not implemented)."""
    return {"note": "twilio_not_configured"}

def call_tellows(number: str) -> Dict[str, Any]:
    """Placeholder for Tellows integration (not implemented)."""
    return {"note": "tellows_not_configured"}


##### Geocoding (city -> lat/lng) #####
def geocode_city(city: str, country: str) -> Optional[Dict[str, float]]:
    if not OPENCAGE_KEY or not city:
        return None
    q = f"{city}, {country}" if country else city
    params = {"q": q, "key": OPENCAGE_KEY, "limit": 1}
    try:
        r = requests.get(OPENCAGE_URL, params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data.get("results"):
            geo = data["results"][0]["geometry"]
            return {"lat": geo["lat"], "lng": geo["lng"]}
    except Exception:
        return None
    return None


##### Reputation aggregator (simple heuristics + user reports) #####
def heuristics_score(numverify: Dict[str, Any]) -> float:
    score = 0.0
    lt = (numverify.get("line_type") or "").lower()
    carrier = (numverify.get("carrier") or "").lower()

    if "premium rate" in lt or "premium_rate" in lt:
        score = max(score, 90)
    elif "satellite" in lt:
        score = max(score, 80)
    elif "voip" in lt:
        score = max(score, 45)
    elif "unknown" in lt or lt == "":
        score = max(score, 30)
    elif "mobile" in lt:
        score = max(score, 10)
    elif "landline" in lt:
        score = max(score, 5)

    suspicious_carrier_keywords = ["premium", "scam", "telemarketing", "spam"]
    for kw in suspicious_carrier_keywords:
        if kw in carrier:
            score = max(score, 70)

    # small example: suspicious prefix heuristic
    e164 = (numverify.get("international") or "") or (numverify.get("e164") or "")
    if isinstance(e164, str) and e164.startswith("+91140"):
        score = min(100, score + 30)

    return float(score)


def aggregate_reputation(number: str,
                         numverify: Dict[str, Any],
                         fraud: Dict[str, Any],
                         twilio: Dict[str, Any],
                         tellows: Dict[str, Any],
                         user_report_count: int = 0) -> Dict[str, Any]:
    """
    Combine heuristics + optional provider scores into a 0..100 reputation.
    The breakdown shows each source -> score and the configured weight.
    """
    sources = {}
    heur = heuristics_score(numverify or {})
    sources["heuristics"] = {"score": heur, "weight": 0.7, "note": "line_type/carrier/prefix rules"}

    # If fraud contains a numeric score, map it (this is placeholder logic)
    if isinstance(fraud, dict) and "fraud_score" in fraud:
        try:
            f = float(fraud["fraud_score"])
            sources["fraudscore"] = {"score": f, "weight": 0.2}
        except Exception:
            pass

    # tellows placeholder mapping (if present)
    if isinstance(tellows, dict) and "score" in tellows:
        t = tellows.get("score")
        try:
            tscore = (float(t) - 1.0) / 8.0 * 100.0
            sources["tellows"] = {"score": tscore, "weight": 0.1}
        except Exception:
            pass

    # user reports
    if user_report_count > 0:
        rep_score = min(90, user_report_count * 30)
        sources["user_reports"] = {"score": rep_score, "weight": 0.3}

    # normalize weights
    total_weight = sum(v["weight"] for v in sources.values()) or 1.0
    overall = sum(v["score"] * (v["weight"] / total_weight) for v in sources.values())
    label = "clean"
    if overall >= 75:
        label = "spam"
    elif overall >= 40:
        label = "suspicious"

    breakdown = {k: {"score": v["score"], "weight": v["weight"]} for k, v in sources.items()}
    return {"score": round(overall, 1), "label": label, "breakdown": breakdown}


##### DB helpers #####
def save_lookup_to_db(doc: Dict[str, Any]) -> None:
    db = get_db()
    # Update one document per number and push history snapshot
    db.lookups.update_one(
        {"number": doc["number"]},
        {"$set": {**doc, "last_lookup_ts": int(time.time())}, "$push": {"history": {"ts": int(time.time()), "snapshot": doc}}},
        upsert=True,
    )

def add_favorite(number: str, note: str = ""):
    db = get_db()
    db.favorites.update_one({"number": number}, {"$set": {"number": number, "note": note, "ts": int(time.time())}}, upsert=True)

def remove_favorite(number: str):
    db = get_db()
    db.favorites.delete_one({"number": number})

def list_history(limit: int = 100, filter_q: Dict[str, Any] = None):
    db = get_db()
    q = filter_q or {}
    cursor = db.lookups.find(q).sort("last_lookup_ts", -1).limit(limit)
    return list(cursor)

def list_favorites():
    db = get_db()
    return list(db.favorites.find().sort("ts", -1))


##### PDF / phone extraction helpers #####
def extract_numbers_from_text(text: str, default_region: Optional[str] = None) -> List[str]:
    """
    Use phonenumbers.PhoneNumberMatcher to find phone numbers in text and
    return them in E.164 when possible (falls back to raw match).
    """
    results = []
    if not text:
        return results
    for match in PhoneNumberMatcher(text, default_region or None):
        try:
            num = format_number(match.number, PhoneNumberFormat.E164)
            results.append(num)
        except Exception:
            results.append(match.raw_string)
    # unique preserve order
    seen = set()
    out = []
    for n in results:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

def extract_numbers_from_pdf(file_stream) -> List[str]:
    """
    file_stream: file-like object (werkzeug FileStorage .stream)
    """
    try:
        reader = PdfReader(file_stream)
        text = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
                text.append(page_text)
            except Exception:
                continue
        joined = "\n".join(text)
        # try extracting using no default region (find international)
        nums = extract_numbers_from_text(joined, default_region=None)
        return nums
    except Exception:
        return []


##### Export helpers #####
def generate_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    import csv, io
    si = io.StringIO()
    w = csv.writer(si)
    w.writerow(["number", "country", "carrier", "location", "score", "label", "last_lookup_ts"])
    for r in rows:
        nv = r.get("numverify", {})
        rep = r.get("reputation", {})
        w.writerow([
            r.get("number"),
            nv.get("country_name"),
            nv.get("carrier"),
            nv.get("location"),
            rep.get("score"),
            rep.get("label"),
            r.get("last_lookup_ts") or ""
        ])
    return si.getvalue().encode("utf-8")

def generate_pdf_bytes(rows: List[Dict[str, Any]]) -> bytes:
    """
    Simple PDF generator using reportlab. Returns PDF bytes.
    """
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 40
    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y, "Phone Lookup Report")
    p.setFont("Helvetica", 10)
    y -= 30
    for r in rows:
        if y < 80:
            p.showPage()
            y = height - 40
            p.setFont("Helvetica", 10)
        nv = r.get("numverify", {})
        rep = r.get("reputation", {})
        p.drawString(40, y, f"Number: {r.get('number')} — {nv.get('country_name','')} / {nv.get('carrier','')}")
        y -= 12
        p.drawString(60, y, f"Location: {nv.get('location','')} | Score: {rep.get('score')} ({rep.get('label')})")
        y -= 18
    p.save()
    buffer.seek(0)
    return buffer.read()
