"""One-time / occasional: add restaurants to track.

Since there's no API to read a Google Maps saved list directly, seed this
from a plain text file -- easiest path is exporting your list via Google
Takeout (Maps > "Your places"), or just copy-pasting names out of the app.

Usage:
    python seed.py restaurants.txt

Where restaurants.txt has one restaurant per line, e.g.:
    Lilia, Brooklyn NY
    Don Angie
    Katz's Delicatessen, Manhattan
"""
import sys
from db import init_db, add_restaurant
from places_client import find_place_id, place_summary


def seed_from_file(path):
    init_db()
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]

    for line in lines:
        if "," in line:
            name, hint = line.split(",", 1)
        else:
            name, hint = line, ""
        place = find_place_id(name.strip(), hint.strip())
        if not place:
            print(f"  ✗ no match found for: {line}")
            continue
        fields = place_summary(place, fallback_name=name.strip())
        add_restaurant(**fields)
        print(f"  ✓ added: {fields['name']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python seed.py restaurants.txt")
        sys.exit(1)
    seed_from_file(sys.argv[1])
