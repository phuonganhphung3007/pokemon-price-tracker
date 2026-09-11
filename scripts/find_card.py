#!/usr/bin/env python3
"""
One-off helper to find a card's tcgPlayerId so you can add it to watchlist.json.

This calls GET /cards?search=... which bills `limit` credits (default 5 here),
so only run it when adding new cards -- not as part of the daily job.

Usage:
  PPT_API_KEY=xxx python scripts/find_card.py "Charizard VMAX" --set "Champion's Path"
  PPT_API_KEY=xxx python scripts/find_card.py "Pikachu" --limit 10
"""
import argparse
import os
import sys

import requests

BASE_URL = "https://www.pokemonpricetracker.com/api/v2"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="Card name to search for")
    parser.add_argument("--set", dest="set_name", default=None, help="Optional set name filter")
    parser.add_argument("--limit", type=int, default=5, help="Max results (billed at 1 credit/result)")
    args = parser.parse_args()

    api_key = os.environ.get("PPT_API_KEY")
    if not api_key:
        sys.exit("Set PPT_API_KEY in your environment first.")

    params = {"search": args.name, "limit": args.limit}
    if args.set_name:
        params["search"] = f"{args.name} {args.set_name}"

    resp = requests.get(
        f"{BASE_URL}/cards",
        headers={"Authorization": f"Bearer {api_key}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    cards = payload.get("data", [])
    if not cards:
        print("No matches found.")
        return

    print(f"{'tcgPlayerId':<14} {'name':<40} {'set':<30} market")
    print("-" * 100)
    for c in cards:
        prices = c.get("prices", {}) or {}
        set_info = c.get("set", {}) or {}
        print(
            f"{c.get('tcgPlayerId', ''):<14} "
            f"{(c.get('name') or '')[:38]:<40} "
            f"{(set_info.get('name') or '')[:28]:<30} "
            f"{prices.get('market')}"
        )

    used = payload.get("metadata", {}).get("apiCallsConsumed")
    print(f"\nCredits used for this search: {used}")
    print('Copy the tcgPlayerId you want into watchlist.json, e.g.:')
    print('  { "tcgPlayerId": 114004, "label": "Charizard VMAX (Champion\'s Path)" }')


if __name__ == "__main__":
    main()
