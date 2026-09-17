"""Tests for seed.py's wiring, not its network half.

seed.py is a CLI, and the only thing about it that has actually broken is the
part that runs before any restaurant is looked up: it never called
`load_dotenv()`. Nothing further down its import chain does either --
`places_client` reads `GOOGLE_PLACES_API_KEY` out of the environment at call
time -- so seeding failed with "set GOOGLE_PLACES_API_KEY in your environment
/ .env" for a key that was sitting in `.env` the whole time. main.py and
app.py both load it; seed.py was the one entry point that didn't.

The Places call itself stays untested here for the reason the plan gives:
faking `requests` at that seam would only assert the mock.
"""
import importlib

import dotenv

import db
import places_client
import seed


def test_seed_loads_the_env_file_at_import(monkeypatch):
    """The regression itself. Asserted on the module's import side effect,
    because that's where it has to happen -- by the time seed_from_file()
    reaches find_place_id(), places_client has already read the environment."""
    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: calls.append(True))

    importlib.reload(seed)

    assert calls, "seed.py must load .env -- it's the only entry point that reads a key"


def test_seeded_restaurants_are_not_verified(tmp_path, monkeypatch):
    """Seeding resolves a file of names with nobody looking at any of them,
    which is the whole reason the verification gate exists. If these landed
    verified, run_check would start checking place_ids no one had ever seen."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(
        places_client, "find_place_id",
        lambda name, hint="": {"id": f"place-{name.strip().lower()}",
                               "displayName": {"text": name.strip()},
                               "formattedAddress": "somewhere"},
    )
    monkeypatch.setattr(seed, "find_place_id", places_client.find_place_id)
    listing = tmp_path / "restaurants.txt"
    listing.write_text("Lilia, Brooklyn NY\nDon Angie\n", encoding="utf-8")

    seed.seed_from_file(str(listing))

    rows = db.list_restaurants(active_only=False)
    assert sorted(r["name"] for r in rows) == ["Don Angie", "Lilia"]
    assert all(r["verified_at"] is None for r in rows)


def test_a_line_with_no_match_is_skipped_not_fatal(tmp_path, monkeypatch):
    """One unresolvable name in a long file shouldn't cost the rest of it."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(
        seed, "find_place_id",
        lambda name, hint="": None if name.strip() == "Nowhere" else {
            "id": "place-lilia", "displayName": {"text": "Lilia"},
            "formattedAddress": "567 Union Ave"},
    )
    listing = tmp_path / "restaurants.txt"
    listing.write_text("Nowhere\nLilia, Brooklyn NY\n", encoding="utf-8")

    seed.seed_from_file(str(listing))

    assert [r["name"] for r in db.list_restaurants(active_only=False)] == ["Lilia"]
