#!/usr/bin/env python3
"""
Daily fetch job for pokemonpricetracker.com API.

Reads watchlist.json, pulls current price + 1-condition history for each
card via GET /cards/{tcgPlayerId}, and writes results into:
  docs/data/latest.json   -> current snapshot per card (for the dashboard header)
  docs/data/history.csv   -> append-only daily price/volume series (for charts)

Designed to run once per day (prices/volume refresh once/day upstream, so
running more often just burns credits without new data). Stays inside the
free plan's 100 credits/day budget by using limit=1 single-card lookups
(2 credits/card: 1 base + 1 for includeHistory) and stopping early if the
budget would be exceeded.
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "watchlist.json"
DATA_DIR = ROOT / "docs" / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.csv"

BASE_URL = "https://www.pokemonpricetracker.com/api/v2"
API_KEY = os.environ.get("PPT_API_KEY")

# Safety margin under the free plan's 100 credits/day cap.
MAX_DAILY_CREDITS = int(os.environ.get("MAX_DAILY_CREDITS", "95"))
# Small pause between calls; well under the 60 calls/min limit either way.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "1.0"))
# How many days of history to pull each call. Small window is enough since
# we run daily and append -- we're not trying to backfill in one shot.
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "3"))

CSV_FIELDS = ["date", "tcgPlayerId", "label", "printing", "condition", "price", "volume"]


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        sys.exit(f"Missing {WATCHLIST_PATH}. Add at least one card first.")
    raw = json.loads(WATCHLIST_PATH.read_text())
    cards = raw.get("cards", [])
    if not cards:
        sys.exit("watchlist.json has no cards listed under 'cards'.")
    return cards


def fetch_card(tcg_player_id: int) -> dict:
    url = f"{BASE_URL}/cards/{tcg_player_id}"
    params = {"includeHistory": "true", "days": HISTORY_DAYS}
    headers = {"Authorization": f"Bearer {API_KEY}"}

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        retry_after = int(resp.headers.get("Retry-After", "60"))
        limit_type = body.get("limitType", "unknown")
        if limit_type == "daily":
            print(f"  Daily credit limit hit (resets {body.get('resetsAt')}). Stopping run.")
            raise DailyLimitReached()
        print(f"  Per-minute limit hit, waiting {retry_after}s then retrying...")
        time.sleep(retry_after)
        return fetch_card(tcg_player_id)

    resp.raise_for_status()
    return resp.json()


class DailyLimitReached(Exception):
    pass


def extract_rows(payload: dict, tcg_player_id: int, label: str) -> tuple[list[dict], dict]:
    """Returns (history_rows, latest_snapshot) for one card's API response."""
    card = payload.get("data")
    if isinstance(card, list):
        card = card[0] if card else {}
    card = card or {}

    prices = card.get("prices", {}) or {}
    market = prices.get("market")
    primary_printing = prices.get("primaryPrinting")

    price_history = card.get("priceHistory", {}) or {}
    conditions = price_history.get("conditions", {}) or {}

    rows = []
    for condition, series in conditions.items():
        for point in series.get("history", []) or []:
            rows.append({
                "date": point.get("date"),
                "tcgPlayerId": tcg_player_id,
                "label": label,
                "printing": primary_printing,
                "condition": condition,
                "price": point.get("price") if point.get("price") is not None else point.get("market"),
                "volume": point.get("volume"),
            })

    snapshot = {
        "tcgPlayerId": tcg_player_id,
        "label": label,
        "name": card.get("name", label),
        "market": market,
        "primaryPrinting": primary_printing,
        "setName": (card.get("set") or {}).get("name") if isinstance(card.get("set"), dict) else None,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
    }
    print(rows, snapshot)
    return rows, snapshot


def merge_history(new_rows: list[dict]) -> None:
    """Append new rows, de-duplicating on (date, tcgPlayerId, condition)."""
    existing = {}
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, newline="") as f:
            for row in csv.DictReader(f):
                key = (row["date"], row["tcgPlayerId"], row["condition"])
                existing[key] = row

    for row in new_rows:
        if row["date"] is None or row["price"] is None:
            continue
        key = (str(row["date"]), str(row["tcgPlayerId"]), row["condition"])
        existing[key] = {k: ("" if v is None else v) for k, v in row.items()}

    ordered = sorted(existing.values(), key=lambda r: (r["date"], r["label"], r["condition"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(ordered)


def main():
    if not API_KEY:
        sys.exit("PPT_API_KEY environment variable is not set.")

    cards = load_watchlist()
    print(f"Tracking {len(cards)} card(s). History window: {HISTORY_DAYS} days.")

    all_new_rows = []
    latest_snapshots = {}
    total_credits_used = 0

    for card in cards:
        tcg_id = card["tcgPlayerId"]
        label = card.get("label", str(tcg_id))

        # Bail before the call if it would plausibly blow the daily budget.
        estimated_next_cost = 2  # 1 base + 1 includeHistory
        if total_credits_used + estimated_next_cost > MAX_DAILY_CREDITS:
            print(f"Stopping before '{label}': would exceed MAX_DAILY_CREDITS ({MAX_DAILY_CREDITS}).")
            break

        print(f"Fetching {label} (tcgPlayerId={tcg_id})...")
        try:
            payload = fetch_card(tcg_id)
        except DailyLimitReached:
            break
        except requests.HTTPError as e:
            print(f"  Request failed: {e}")
            continue

        used = payload.get("metadata", {}).get("apiCallsConsumed", {})
        used_total = used.get("total", 2) if isinstance(used, dict) else used
        total_credits_used += used_total or 0
        print(f"  OK. Credits used this call: {used_total}. Running total: {total_credits_used}")

        rows, snapshot = extract_rows(payload, tcg_id, label)
        all_new_rows.extend(rows)
        latest_snapshots[str(tcg_id)] = snapshot

        time.sleep(REQUEST_DELAY_SECONDS)

    merge_history(all_new_rows)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing_latest = {}
    if LATEST_PATH.exists():
        existing_latest = json.loads(LATEST_PATH.read_text())
    existing_latest.update(latest_snapshots)
    existing_latest["_meta"] = {
        "lastRun": datetime.now(timezone.utc).isoformat(),
        "creditsUsedThisRun": total_credits_used,
    }
    LATEST_PATH.write_text(json.dumps(existing_latest, indent=2, sort_keys=True))

    print(f"Done. {len(all_new_rows)} history points written. {total_credits_used} credits used total.")


if __name__ == "__main__":
    main()
