# backend/app.py
import os
import time
import io
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

from utils import (
    call_numverify, call_fraudscore, call_twilio_lookup,
    call_tellows, geocode_city, aggregate_reputation,
    cache_get, cache_set, save_lookup_to_db, extract_numbers_from_pdf,
    generate_csv_bytes, generate_pdf_bytes, list_history, list_favorites,
    add_favorite, remove_favorite, get_db
)

app = Flask(__name__)
CORS(app)


def lookup_number(number: str):
    cache_key = f"fullint:{number}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    numv = call_numverify(number)
    fraud = call_fraudscore(number)
    tw = call_twilio_lookup(number)
    tellows = call_tellows(number)
    user_reports_count = 0

    agg = aggregate_reputation(number, numv, fraud, tw, tellows, user_reports_count)
    coords = geocode_city(numv.get("location") or "", numv.get("country_name") or "")

    result = {
        "number": number,
        "numverify": numv,
        "fraudscore": fraud,
        "twilio": tw,
        "tellows": tellows,
        "user_reports_count": user_reports_count,
        "reputation": agg,
        "coordinates": coords,
        "last_lookup_ts": int(time.time())
    }
    cache_set(cache_key, result)
    return result


@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    body = request.get_json() or {}
    number = body.get("phone")
    if not number:
        return jsonify({"error": "phone required"}), 400
    res = lookup_number(number)
    save_lookup_to_db(res)
    return jsonify(res)


@app.route("/api/batch-lookup", methods=["POST"])
def api_batch_lookup():
    if "file" in request.files:
        f = request.files["file"]
        data = f.read().decode("utf-8").splitlines()
        import csv
        reader = csv.reader(data)
        results = []
        for row in reader:
            if not row:
                continue
            number = row[0].strip()
            if not number:
                continue
            res = lookup_number(number)
            save_lookup_to_db(res)
            results.append(res)
        return jsonify({"count": len(results), "results": results})
    else:
        body = request.get_json() or {}
        numbers = body.get("numbers", [])
        results = []
        for number in numbers:
            res = lookup_number(number)
            save_lookup_to_db(res)
            results.append(res)
        return jsonify({"count": len(results), "results": results})


@app.route("/api/upload-pdf", methods=["POST"])
def api_upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    nums = extract_numbers_from_pdf(f.stream)
    results = []
    for n in nums:
        res = lookup_number(n)
        save_lookup_to_db(res)
        results.append(res)
    return jsonify({"count": len(results), "numbers": nums, "results": results})


@app.route("/api/history", methods=["GET"])
def api_history():
    country = request.args.get("country")
    carrier = request.args.get("carrier")
    minscore = request.args.get("minscore")
    limit = int(request.args.get("limit", 100))
    q = {}
    if country:
        q["numverify.country_name"] = country
    if carrier:
        q["numverify.carrier"] = carrier
    rows = list_history(limit=limit, filter_q=q)
    for r in rows:
        r["_id"] = str(r.get("_id"))
    return jsonify({"count": len(rows), "results": rows})


@app.route("/api/favorites", methods=["GET", "POST", "DELETE"])
def api_favorites():
    if request.method == "GET":
        rows = list_favorites()
        for r in rows:
            r["_id"] = str(r.get("_id"))
        return jsonify({"count": len(rows), "results": rows})
    elif request.method == "POST":
        body = request.get_json() or {}
        number = body.get("phone")
        note = body.get("note", "")
        if not number:
            return jsonify({"error": "phone required"}), 400
        add_favorite(number, note)
        return jsonify({"ok": True})
    else:  # DELETE
        number = request.args.get("phone")
        if not number:
            return jsonify({"error": "phone required"}), 400
        remove_favorite(number)
        return jsonify({"ok": True})


@app.route("/api/export", methods=["GET", "POST"])
def api_export():
    """
    GET  -> /api/export?format=csv
    POST -> { "numbers": [...], "format": "pdf|csv|json" }
    """
    db = get_db()

    # Handle GET request
    if request.method == "GET":
        fmt = request.args.get("format", "json").lower()
        rows = list(db.lookups.find().sort("last_lookup_ts", -1))
        for r in rows:
            r["_id"] = str(r.get("_id"))
        if fmt == "json":
            return jsonify(rows)
        elif fmt == "csv":
            b = generate_csv_bytes(rows)
            return send_file(io.BytesIO(b), mimetype="text/csv", as_attachment=True, download_name="history.csv")
        elif fmt == "pdf":
            b = generate_pdf_bytes(rows)
            return send_file(io.BytesIO(b), mimetype="application/pdf", as_attachment=True, download_name="history.pdf")
        return jsonify({"error": "unsupported format"}), 400

    # Handle POST request
    body = request.get_json() or {}
    numbers = body.get("numbers", [])
    fmt = (body.get("format") or "json").lower()
    query = {}
    if numbers:
        query = {"number": {"$in": numbers}}
    rows = list(db.lookups.find(query).sort("last_lookup_ts", -1))
    for r in rows:
        r["_id"] = str(r.get("_id"))

    if fmt == "json":
        return jsonify(rows)
    elif fmt == "csv":
        b = generate_csv_bytes(rows)
        return send_file(io.BytesIO(b), mimetype="text/csv", as_attachment=True, download_name="report.csv")
    elif fmt == "pdf":
        b = generate_pdf_bytes(rows)
        return send_file(io.BytesIO(b), mimetype="application/pdf", as_attachment=True, download_name="report.pdf")
    return jsonify({"error": "unsupported format"}), 400

if __name__ == "__main__":
    app.run(debug=True, port=5000)