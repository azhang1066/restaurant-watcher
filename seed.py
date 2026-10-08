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
import logging
import sys

from dotenv import load_dotenv

from db import init_db, add_restaurant
from places_client import find_place_id, place_summary

logger = logging.getLogger(__name__)

# Same as main.py and app.py do at import. Nothing further down the import
# chain loads it -- places_client reads GOOGLE_PLACES_API_KEY out of the
# environment at call time.
load_dotenv()


def seed_from_file(path):
    init_db()
    with open(path) as f:
        lines = [line.strip() for line in f if line.strip()]

    logger.info("Resolving %d restaurant(s) through Google Places...", len(lines))
    for line in lines:
        if "," in line:
            name, hint = line.split(",", 1)
        else:
            name, hint = line, ""
        place = find_place_id(name.strip(), hint.strip())
        if not place:
            logger.warning("  ✗ no match found for: %s", line)
            continue
        fields = place_summary(place, fallback_name=name.strip())
        add_restaurant(**fields)
        logger.info("  ✓ added: %s — %s", fields["name"], fields["address"] or "no address")


if __name__ == "__main__":
    # Bare messages: this is a CLI's progress output, not a log.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2:
        sys.exit("Usage: python seed.py restaurants.txt")
    seed_from_file(sys.argv[1])
