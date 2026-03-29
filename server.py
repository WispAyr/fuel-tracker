"""Fuel Price Tracker for Ayr & Ayrshire — FastAPI backend with caching proxy."""

import argparse
import hashlib
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Fuel Price Tracker — Ayr & Ayrshire")

BASE_URL = "https://fuelcosts.co.uk/api"
DEFAULT_LAT = 55.458
DEFAULT_LON = -4.629
DEFAULT_RADIUS = 20
CACHE_TTL = 300  # 5 minutes

UK_TZ = ZoneInfo("Europe/London")

_cache: dict[str, tuple[float, any]] = {}

DB_PATH = Path(__file__).parent / "sightings.db"

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ── SQLite Sightings DB ────────────────────────────────

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            has_fuel INTEGER NOT NULL,
            queue_length TEXT NOT NULL,
            note TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_station_ts ON sightings(station_id, timestamp)")
    conn.commit()
    conn.close()


_init_db()


# ── Cache helpers ──────────────────────────────────────

def _cache_key(url: str, params: dict | None = None) -> str:
    parts = [url]
    if params:
        parts.extend(f"{k}={v}" for k, v in sorted(params.items()))
    return "|".join(parts)


async def _fetch(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    key = _cache_key(url, params)
    now = time.time()
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < CACHE_TTL:
            return data
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        _cache[key] = (now, data)
        return data


# ── Opening Hours ──────────────────────────────────────

def _parse_time(ts: str) -> tuple[int, int]:
    """Parse HH:MM:SS or HH:MM → (hour, minute)."""
    parts = ts.split(":")
    return int(parts[0]), int(parts[1])


def _format_time_ampm(hour: int, minute: int) -> str:
    """Format 24h hour/minute to 12h AM/PM."""
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12 or 12
    if minute == 0:
        return f"{h}:00 {suffix}"
    return f"{h}:{minute:02d} {suffix}"


def _compute_open_status(station: dict) -> dict:
    """Compute is_open_now, open_status, and next_change for a station."""
    if station.get("is_permanently_closed"):
        return {"is_open_now": False, "open_status": "Permanently Closed", "next_change": None}
    if station.get("is_temporarily_closed"):
        return {"is_open_now": False, "open_status": "Temporarily Closed", "next_change": None}

    opening_times = station.get("opening_times") or {}
    usual_days = opening_times.get("usual_days") or {}

    if not usual_days:
        return {"is_open_now": None, "open_status": "Hours unknown", "next_change": None}

    now_uk = datetime.now(UK_TZ)
    # Python weekday(): 0=Monday … 6=Sunday
    today_name = DAY_NAMES[now_uk.weekday()]
    today_info = usual_days.get(today_name) or {}

    if today_info.get("is_24_hours"):
        # Check if every day is 24h
        all_24 = all((v or {}).get("is_24_hours") for v in usual_days.values())
        if all_24:
            return {"is_open_now": True, "open_status": "Open 24hrs", "next_change": None}
        # 24h today but not always
        return {"is_open_now": True, "open_status": "Open 24hrs today", "next_change": None}

    if not today_info.get("open") or not today_info.get("close"):
        # No hours for today → closed
        # Find next open day
        for i in range(1, 8):
            day_idx = (now_uk.weekday() + i) % 7
            day_name = DAY_NAMES[day_idx]
            day_info = usual_days.get(day_name) or {}
            if day_info.get("is_24_hours") or (day_info.get("open") and day_info.get("close")):
                if day_info.get("is_24_hours"):
                    open_str = "midnight"
                else:
                    oh, om = _parse_time(day_info["open"])
                    open_str = _format_time_ampm(oh, om)
                day_label = day_name.capitalize() if i > 1 else "tomorrow"
                if i == 1:
                    day_label = "tomorrow"
                return {
                    "is_open_now": False,
                    "open_status": f"Closed — Opens {open_str} {day_label}",
                    "next_change": {"type": "opens", "day": day_name, "time": open_str},
                }
        return {"is_open_now": False, "open_status": "Closed today", "next_change": None}

    # Parse today's open/close
    open_h, open_m = _parse_time(today_info["open"])
    close_h, close_m = _parse_time(today_info["close"])

    now_minutes = now_uk.hour * 60 + now_uk.minute
    open_minutes = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m

    if open_minutes <= now_minutes < close_minutes:
        # Open — how long until close?
        mins_to_close = close_minutes - now_minutes
        close_str = _format_time_ampm(close_h, close_m)
        status = f"Open until {close_str}"
        return {
            "is_open_now": True,
            "open_status": status,
            "next_change": {"type": "closes", "minutes": mins_to_close, "time": close_str},
        }
    elif now_minutes < open_minutes:
        # Not yet open today
        open_str = _format_time_ampm(open_h, open_m)
        return {
            "is_open_now": False,
            "open_status": f"Closed — Opens {open_str}",
            "next_change": {"type": "opens", "time": open_str, "minutes": open_minutes - now_minutes},
        }
    else:
        # Closed for the day — find next open
        for i in range(1, 8):
            day_idx = (now_uk.weekday() + i) % 7
            day_name = DAY_NAMES[day_idx]
            day_info = usual_days.get(day_name) or {}
            if day_info.get("is_24_hours") or (day_info.get("open") and day_info.get("close")):
                if day_info.get("is_24_hours"):
                    open_str = "midnight"
                else:
                    oh, om = _parse_time(day_info["open"])
                    open_str = _format_time_ampm(oh, om)
                day_label = "tomorrow" if i == 1 else day_name.capitalize()
                return {
                    "is_open_now": False,
                    "open_status": f"Closed — Opens {open_str} {day_label}",
                    "next_change": {"type": "opens", "day": day_name, "time": open_str},
                }
        return {"is_open_now": False, "open_status": "Closed", "next_change": None}


# ── Freshness ──────────────────────────────────────────

def _station_freshness(station: dict) -> dict:
    """Compute freshness status for a station."""
    now = datetime.now(timezone.utc)

    if station.get("is_permanently_closed"):
        return {"status": "closed", "label": "Permanently Closed", "icon": "⚫", "hours_since_update": None}
    if station.get("is_temporarily_closed"):
        return {"status": "closed", "label": "Temporarily Closed", "icon": "⚫", "hours_since_update": None}

    # Find most recent price update across all fuel types
    latest = None
    for p in station.get("prices", []):
        ts_str = p.get("price_last_updated")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if latest is None or ts > latest:
                latest = ts
        except (ValueError, TypeError):
            continue

    if latest is None:
        return {"status": "stale", "label": "No price data", "icon": "🔴", "hours_since_update": None, "detail": "No price data available"}

    hours = (now - latest).total_seconds() / 3600

    # UK Gov Fuel Finder: stations MUST update within 30 mins of any price change
    if hours < 1:
        return {"status": "fresh", "label": "Verified", "icon": "🟢", "hours_since_update": round(hours, 1),
                "detail": "Updated within the hour — confirmed open with fuel"}
    elif hours < 24:
        return {"status": "fresh", "label": "Fresh", "icon": "🟢", "hours_since_update": round(hours, 1),
                "detail": "Recently updated — likely operating normally"}
    elif hours < 48:
        return {"status": "aging", "label": "Aging", "icon": "🟡", "hours_since_update": round(hours, 1),
                "detail": "Not updated in 24h+ — price unchanged or station may be closed"}
    elif hours < 72:
        return {"status": "stale", "label": "Unverified", "icon": "🔴", "hours_since_update": round(hours, 1),
                "detail": "Not updated in 48h+ — suspicious, possibly out of stock"}
    else:
        return {"status": "stale", "label": "Likely closed", "icon": "🔴", "hours_since_update": round(hours, 1),
                "detail": "Not updated in 72h+ — likely closed or out of fuel"}


# ── Supply Health ──────────────────────────────────────

def _supply_health(stations: list) -> dict:
    """Calculate area supply health score. Only counts OPEN stations."""
    # Filter to open stations only for health calculation
    open_stations = [s for s in stations if s.get("open_status_data", {}).get("is_open_now") is not False]
    total = len(stations)
    total_open = len(open_stations)

    if total == 0:
        return {"score": 0, "status": "alert", "label": "NO DATA", "emoji": "🚨",
                "fresh": 0, "aging": 0, "stale": 0, "closed": 0, "total": 0,
                "total_open": 0, "fresh_pct": 0, "closed_pct": 0}

    fresh = aging = stale = closed_count = 0
    for s in stations:
        f = _station_freshness(s)
        if f["status"] == "fresh":
            fresh += 1
        elif f["status"] == "aging":
            aging += 1
        elif f["status"] == "closed":
            closed_count += 1
        else:
            stale += 1

    # Health based on open stations only
    base = total_open if total_open > 0 else total
    fresh_pct = (fresh / base) * 100
    closed_pct = (closed_count / total) * 100

    if fresh_pct > 80 and closed_pct < 5:
        status, label, emoji = "normal", "NORMAL", "✅"
    elif fresh_pct < 60 or closed_pct > 15:
        status, label, emoji = "alert", "ALERT", "🚨"
    else:
        status, label, emoji = "watch", "WATCH", "⚠️"

    score = round(fresh_pct * 0.8 + (100 - closed_pct * 5) * 0.2)
    score = max(0, min(100, score))

    return {
        "score": score, "status": status, "label": label, "emoji": emoji,
        "fresh": fresh, "aging": aging, "stale": stale, "closed": closed_count,
        "total": total, "total_open": total_open,
        "fresh_pct": round(fresh_pct, 1), "closed_pct": round(closed_pct, 1),
    }


# ── Price Trend Analysis ───────────────────────────────

def _price_trend_analysis(trends: list) -> dict:
    """Analyse price trend data for anomalies and direction."""
    if not trends or len(trends) < 2:
        return {"direction": "stable", "symbol": "→", "change_7d": 0, "anomaly": False, "description": "Insufficient data"}

    recent = trends[-1]
    recent_price = recent.get("avgPricePence", 0)

    # 7-day change
    week_ago_price = None
    recent_date = recent.get("date", "")
    for t in reversed(trends):
        if t.get("date", "") <= recent_date:
            try:
                rd = datetime.fromisoformat(recent_date)
                td = datetime.fromisoformat(t.get("date", recent_date))
                if (rd - td).days >= 6:
                    week_ago_price = t.get("avgPricePence")
                    break
            except (ValueError, TypeError):
                continue

    if week_ago_price is None and len(trends) >= 2:
        week_ago_price = trends[0].get("avgPricePence", recent_price)

    change_7d = round(recent_price - (week_ago_price or recent_price), 1)

    if change_7d > 2:
        direction, symbol = "rising", "↑"
    elif change_7d < -2:
        direction, symbol = "falling", "↓"
    else:
        direction, symbol = "stable", "→"

    # Anomaly: check for sudden spikes (>5p in consecutive readings)
    anomaly = False
    for i in range(1, len(trends)):
        prev = trends[i - 1].get("avgPricePence", 0)
        curr = trends[i].get("avgPricePence", 0)
        if curr - prev > 5:
            anomaly = True
            break

    desc = f"{symbol} {direction.title()} ({change_7d:+.1f}p/week)"
    if anomaly:
        desc += " — ⚠️ Abnormal spike detected"

    return {"direction": direction, "symbol": symbol, "change_7d": change_7d, "anomaly": anomaly, "description": desc}


# ── Latest Sighting ────────────────────────────────────

def _latest_sighting(station_id: str) -> dict | None:
    """Get the most recent sighting in the last 24h for a station."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    row = conn.execute(
        "SELECT * FROM sightings WHERE station_id=? AND timestamp>? ORDER BY timestamp DESC LIMIT 1",
        (station_id, cutoff)
    ).fetchone()
    conn.close()
    if not row:
        return None
    ts = datetime.fromisoformat(row["timestamp"])
    minutes_ago = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    return {
        "minutes_ago": minutes_ago,
        "has_fuel": bool(row["has_fuel"]),
        "queue_length": row["queue_length"],
        "note": row["note"],
    }


# ── Pages ──────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


# ── API Proxies ────────────────────────────────────────

@app.get("/api/stations")
async def stations(
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    radius: int = DEFAULT_RADIUS,
    fuel: str = "E10",
    sort: str = "price",
):
    data = await _fetch("/stations", {
        "lat": lat, "lon": lon, "radius": radius, "fuel": fuel, "sort": sort,
    })
    # Enrich with freshness + open status + latest sighting
    for s in data.get("stations", []):
        s["freshness"] = _station_freshness(s)
        open_data = _compute_open_status(s)
        s["open_status_data"] = open_data
        s["is_open_now"] = open_data["is_open_now"]
        s["open_status"] = open_data["open_status"]
        s["next_change"] = open_data["next_change"]
        s["latest_sighting"] = _latest_sighting(s.get("node_id", ""))
    # Add supply health
    data["supply_health"] = _supply_health(data.get("stations", []))
    return data


@app.get("/api/history/{node_id}")
async def history(node_id: str, fuel: str = "E10", days: int = 30):
    return await _fetch(f"/stations/{node_id}/history", {"fuel": fuel, "days": days})


@app.get("/api/trends")
async def trends(
    type: str = "city",
    name: str = "Ayr",
    fuel: str = "E10",
    days: int = 90,
):
    data = await _fetch("/prices/history", {
        "type": type, "name": name, "fuel": fuel, "days": days,
    })
    data["analysis"] = _price_trend_analysis(data.get("trends", []))
    return data


@app.get("/api/stats")
async def stats():
    best_day = await _fetch("/stats/best-day")
    brands = await _fetch("/stats/brands")
    return {"best_day": best_day, "brands": brands}


@app.get("/api/crude")
async def crude_oil():
    """Attempt to fetch crude oil price. Falls back to link."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
            if r.status_code == 200:
                fx_data = r.json()
                gbp_rate = fx_data.get("rates", {}).get("GBP", 0.79)
            else:
                gbp_rate = 0.79
        return {
            "available": False,
            "gbp_usd": round(gbp_rate, 4),
            "message": "Live crude prices not available via free API",
            "link": "https://oilprice.com/oil-price-charts/",
            "context": "Brent Crude price directly affects UK fuel prices with a ~2 week lag",
        }
    except Exception:
        return {
            "available": False,
            "link": "https://oilprice.com/oil-price-charts/",
            "message": "Could not fetch crude data",
        }


# ── Community Sightings ────────────────────────────────

class SightingBody(BaseModel):
    station_id: str
    has_fuel: bool
    queue_length: str  # "none" | "short" | "long"
    note: str | None = None


@app.post("/api/sighting")
async def post_sighting(body: SightingBody, request: Request):
    # Validate queue_length
    if body.queue_length not in ("none", "short", "long"):
        raise HTTPException(status_code=422, detail="queue_length must be none/short/long")

    # Hash the IP (don't store raw)
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    real_ip = forwarded.split(",")[0].strip() if forwarded else client_ip
    ip_hash = hashlib.sha256(real_ip.encode()).hexdigest()

    # Rate limit: 1 per station per IP per 15 minutes
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    existing = conn.execute(
        "SELECT id FROM sightings WHERE station_id=? AND ip_hash=? AND timestamp>?",
        (body.station_id, ip_hash, cutoff)
    ).fetchone()

    if existing:
        conn.close()
        raise HTTPException(status_code=429, detail="You've already reported this station recently. Please wait 15 minutes.")

    # Truncate note
    note = (body.note or "").strip()[:100] or None

    now_ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sightings (station_id, timestamp, ip_hash, has_fuel, queue_length, note) VALUES (?,?,?,?,?,?)",
        (body.station_id, now_ts, ip_hash, int(body.has_fuel), body.queue_length, note)
    )
    conn.commit()
    conn.close()

    return {"ok": True, "message": "Thanks! Your report helps others."}


@app.get("/api/sightings")
async def get_sightings(station_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = conn.execute(
        "SELECT timestamp, has_fuel, queue_length, note FROM sightings WHERE station_id=? AND timestamp>? ORDER BY timestamp DESC",
        (station_id, cutoff)
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        ts = datetime.fromisoformat(r["timestamp"])
        mins = int((now - ts).total_seconds() / 60)
        result.append({
            "minutes_ago": mins,
            "has_fuel": bool(r["has_fuel"]),
            "queue_length": r["queue_length"],
            "note": r["note"],
        })
    return {"sightings": result, "count": len(result)}


# ── Nuro integration ───────────────────────────────────

@app.get("/api/nuro")
async def nuro():
    """Compact summary for nuro dashboard integration."""
    stations_data = await _fetch("/stations", {
        "lat": DEFAULT_LAT, "lon": DEFAULT_LON,
        "radius": DEFAULT_RADIUS, "fuel": "E10", "sort": "price",
    })
    all_stations = stations_data.get("stations", [])
    if not all_stations:
        return JSONResponse({"error": "No station data available"}, status_code=503)

    # Enrich with open status
    for s in all_stations:
        open_data = _compute_open_status(s)
        s["open_status_data"] = open_data

    # Cheapest E10 and diesel
    cheapest_e10 = {"price": 9999, "station": "", "updated": ""}
    cheapest_diesel = {"price": 9999, "station": "", "updated": ""}
    e10_prices, diesel_prices = [], []

    for s in all_stations:
        brand = s.get("brand_name") or s.get("brand", "")
        city = (s.get("location") or s.get("address") or {}).get("city", "")
        label = f"{brand} {city}".strip() or s.get("trading_name") or s.get("name", "Unknown")
        for p in s.get("prices", []):
            ft, price = p.get("fuel_type", ""), p.get("price")
            if price is None:
                continue
            updated = p.get("price_last_updated", "")
            if ft == "E10":
                e10_prices.append(price)
                if price < cheapest_e10["price"]:
                    cheapest_e10 = {"price": price, "station": label, "updated": updated}
            elif ft == "B7_STANDARD":
                diesel_prices.append(price)
                if price < cheapest_diesel["price"]:
                    cheapest_diesel = {"price": price, "station": label, "updated": updated}

    avg_e10 = round(sum(e10_prices) / len(e10_prices), 1) if e10_prices else None
    avg_diesel = round(sum(diesel_prices) / len(diesel_prices), 1) if diesel_prices else None

    # Supply health (with open status)
    health = _supply_health(all_stations)

    # Price trend
    try:
        trend_data = await _fetch("/prices/history", {
            "type": "city", "name": "Ayr", "fuel": "E10", "days": 14,
        })
        analysis = _price_trend_analysis(trend_data.get("trends", []))
    except Exception:
        analysis = {"direction": "stable", "change_7d": 0}

    return {
        "cheapest_e10": cheapest_e10,
        "cheapest_diesel": cheapest_diesel,
        "average_e10": avg_e10,
        "average_diesel": avg_diesel,
        "station_count": health["total"],
        "trend": analysis["direction"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "supply_health": health["status"],
        "supply_score": health["score"],
        "fresh_stations": health["fresh"],
        "stale_stations": health["stale"] + health["aging"],
        "closed_stations": health["closed"],
        "total_stations": health["total"],
        "open_stations": health.get("total_open", 0),
        "price_trend": analysis["direction"],
        "avg_price_change_7d": analysis.get("change_7d", 0),
    }


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3971)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
