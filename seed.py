"""One-time / occasional: add restaurants to track.

Since there's no API to read a Google Maps saved list directly, seed this
from a plain text file -- easiest path is exporting your list via Google
Takeout (Maps > "Your places"), or just copy-pasting names out of the app.

Seeded restaurants land unverified: this resolves a whole file with nobody
looking at any of it, and Text Search answers every line with exactly one
guess. Nothing here is checked until it's confirmed on the dashboard -- see
main.run_check().

Usage:
    python seed.py restaurants.txt [--user NAME]

Rows go into one user's list. --user is optional while there is at most one
account (with none yet, the first account made adopts them).

Where restaurants.txt has one restaurant per line, e.g.:
    Lilia, Brooklyn NY
    Don Angie
    Katz's Delicatessen, Manhattan
"""
import logging
import sys

import config  # noqa: F401 -- importing it is what loads .env
from db import (
    add_restaurant,
    count_users,
    get_restaurant_by_place_id,
    get_user_by_name,
    init_db,
    list_restaurants,
    list_users,
)
from places_client import find_place_id, place_summary

logger = logging.getLogger(__name__)


def _parse_line(line: str) -> tuple[str, str]:
    if "," in line:
        name, hint = line.split(",", 1)
        return name.strip(), hint.strip()
    return line, ""


def seed_from_file(path: str, user_id: int | None = None) -> dict:
    """Resolve each line and add it. Returns a dict of counts: added,
    existing, no_match, failed.

    One bad line never costs the rest of the file: a Places error is logged
    and counted, and seeding carries on. Places is billed per search, so a
    line is skipped *without* searching when the file repeats it, or when it
    has no address hint and a restaurant with that exact name (ignoring case)
    is already tracked. Add a city or street to a line to force the search
    for a second location of the same name. Everything is scoped to `user_id`'s
    list, so someone else watching the same place doesn't count as tracked.
    """
    init_db()
    # utf-8-sig: Windows editors prepend a BOM, which would otherwise glue
    # itself onto the first restaurant's name.
    with open(path, encoding="utf-8-sig") as f:
        lines = [line.strip() for line in f if line.strip()]

    known_names = {r["name"].lower()
                   for r in list_restaurants(active_only=False, user_id=user_id)}
    seen_lines = set()
    counts = {"added": 0, "existing": 0, "no_match": 0, "failed": 0}

    logger.info("Resolving %d restaurant(s) through Google Places...", len(lines))
    for line in lines:
        name, hint = _parse_line(line)
        key = (name.lower(), hint.lower())
        if key in seen_lines:
            logger.info("  = repeated in the file, skipped: %s", line)
            continue
        seen_lines.add(key)
        if not hint and name.lower() in known_names:
            logger.info("  = already tracked, skipped (no search): %s", line)
            counts["existing"] += 1
            continue

        try:
            place = find_place_id(name, hint)
        except Exception:
            logger.exception("  ! lookup failed for: %s", line)
            counts["failed"] += 1
            continue
        if not place:
            logger.warning("  ✗ no match found for: %s", line)
            counts["no_match"] += 1
            continue

        fields = place_summary(place, fallback_name=name)
        if get_restaurant_by_place_id(fields["place_id"], user_id) is not None:
            logger.info("  = already tracked: %s", fields["name"])
            counts["existing"] += 1
            continue
        add_restaurant(**fields, user_id=user_id)
        known_names.add(fields["name"].lower())
        counts["added"] += 1
        logger.info("  ✓ added: %s — %s", fields["name"], fields["address"] or "no address")

    logger.info("Done: %(added)d added, %(existing)d already tracked, "
                "%(no_match)d with no match, %(failed)d failed.", counts)
    return counts


if __name__ == "__main__":
    # Bare messages: this is a CLI's progress output, not a log.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = sys.argv[1:]
    username = None
    if len(args) == 3 and args[1] == "--user":
        args, username = args[:1], args[2]
    if len(args) != 1:
        sys.exit("Usage: python seed.py restaurants.txt [--user NAME]")
    init_db()
    if username is not None:
        user = get_user_by_name(username)
        if user is None:
            sys.exit(f"No account named {username!r}.")
        owner = user["id"]
    elif count_users() > 1:
        sys.exit("More than one account exists -- say whose list with --user NAME.")
    else:
        owner = None
        if count_users() == 1:
            owner = next(iter(list_users()))["id"]
    # Non-zero when a lookup errored, so a script can tell "some lines didn't
    # resolve" (normal) from "Places was failing" (re-run it).
    sys.exit(1 if seed_from_file(args[0], owner)["failed"] else 0)
