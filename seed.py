"""One-time / occasional: add restaurants to track.

Since there's no API to read a Google Maps saved list directly, seed this
from a plain text file -- easiest path is exporting your list via Google
Takeout (Maps > "Your places"), or just copy-pasting names out of the app.

Seeded restaurants land unverified: this resolves a whole file with nobody
looking at any of it, and Text Search answers every line with exactly one
guess. Nothing here is checked until it's confirmed on the dashboard -- see
main.run_check().

Usage:
    python seed.py restaurants.txt

Where restaurants.txt has one restaurant per line, e.g.:
    Lilia, Brooklyn NY
    Don Angie
    Katz's Delicatessen, Manhattan
"""
import sys

from dotenv import load_dotenv

from db import init_db, add_restaurant
from places_client import find_place_id, place_summary

# Same as main.py and app.py do at import. Nothing further down the import
# chain loads it -- places_client reads GOOGLE_PLACES_API_KEY out of the
# environment at call time -- so without this, seeding only ever worked if
# you happened to have exported the key in your shell, and otherwise failed
# telling you to set a variable that was already sitting in .env.
load_dotenv()


def seed_from_file(path):
    init_db()
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]

    print(f"Resolving {len(lines)} restaurant(s) through Google Places...")
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
        print(f"  ✓ added: {fields['name']} — {fields['address'] or 'no address'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python seed.py restaurants.txt")
        sys.exit(1)
    seed_from_file(sys.argv[1])
