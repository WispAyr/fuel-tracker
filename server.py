"""Fuel Price Tracker for Ayr & Ayrshire — FastAPI backend with caching proxy."""

import argparse
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Fuel Price Tracker — Ayr & Ayrshire")

BASE_URL = "https://fuelcosts.co.uk/api"
DEFAULT_LAT = 55.458
DEFAULT_LON = -4.629
DEFAULT_RADIUS = 20
CACHE_TTL = 300  # 5 minutes

_cache: dict[str, tuple[float, any]] = {}


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


def _supply_health(stations: list) -> dict:
    """Calculate area supply health score."""
    total = len(stations)
    if total == 0:
        return {"score": 0, "status": "alert", "label": "NO DATA", "emoji": "🚨",
                "fresh": 0, "aging": 0, "stale": 0, "closed": 0, "total": 0, "fresh_pct": 0, "closed_pct": 0}

    fresh = aging = stale = closed = 0
    for s in stations:
        f = _station_freshness(s)
        if f["status"] == "fresh":
            fresh += 1
        elif f["status"] == "aging":
            aging += 1
        elif f["status"] == "closed":
            closed += 1
        else:
            stale += 1

    fresh_pct = (fresh / total) * 100
    closed_pct = (closed / total) * 100

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
        "fresh": fresh, "aging": aging, "stale": stale, "closed": closed,
        "total": total, "fresh_pct": round(fresh_pct, 1), "closed_pct": round(closed_pct, 1),
    }


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
    # Enrich with freshness data
    for s in data.get("stations", []):
        s["freshness"] = _station_freshness(s)
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
            # Try exchangerate.host for GBP/USD rate
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

    # Supply health
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
        # Supply intelligence
        "supply_health": health["status"],
        "supply_score": health["score"],
        "fresh_stations": health["fresh"],
        "stale_stations": health["stale"] + health["aging"],
        "closed_stations": health["closed"],
        "total_stations": health["total"],
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
