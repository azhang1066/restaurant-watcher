"""Tests for the dashboard (app.py).

Most of the dashboard reads, so much of what can break is the *reading*:
timestamps stored as naive UTC being read as local time, the closing-soon
flag and status rendering out of step with what main.py notified on, or
archived rows vanishing from a page that exists to show them.

Un-archiving is the one write, and it gets the scrutiny a state change
deserves: that it flips only `archived`, that it is rejected without a valid
CSRF token, that GET cannot trigger it, and that it is honest about a
permanently-closed row the next run will re-archive.

Same setup as the other tests: a real temp SQLite file via db.DB_PATH, so
the rows these pages render are written and read exactly as in production.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

import app as dashboard
import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-key")
    db.init_db()
    return dashboard.create_app().test_client()


def _csrf(client):
    """Put a known CSRF token in the session and hand it back.

    The app mints tokens lazily, when a template actually renders a form, so
    there is nothing to read back on a page with no archived rows. Seeding
    the session keeps these tests about the POST behaviour rather than the
    markup; test_unarchive_accepts_a_token_from_a_rendered_page covers the
    real round trip through a page.
    """
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    return "test-csrf-token"


def _post_unarchive(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/unarchive", data=data,
                       follow_redirects=follow_redirects)


def _add(name, place_id, **checks):
    """Seed one restaurant, optionally running a check result through it."""
    db.add_restaurant(name, place_id, address=f"{name} address")
    restaurant_id = next(r["id"] for r in db.list_restaurants(active_only=False)
                         if r["place_id"] == place_id)
    if checks:
        db.update_check_result(
            restaurant_id,
            checks.get("status", "OPERATIONAL"),
            checks.get("closing_soon", False),
            checks.get("summary", ""),
        )
    return restaurant_id


def _text(response):
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _stat(body, label):
    """Read one number out of the summary tiles by its label."""
    match = re.search(rf"<b>(\d+)</b><span>{label}</span>", body)
    assert match, f"no {label!r} stat tile in the page"
    return int(match.group(1))


def test_index_lists_restaurants_with_status(client):
    _add("Lilia", "place-lilia")
    _add("Don Angie", "place-angie", status="CLOSED_TEMPORARILY")

    body = _text(client.get("/"))
    assert "Lilia" in body
    assert "Don Angie" in body
    assert "Closed temporarily" in body


def test_index_shows_empty_state_before_seeding(client):
    body = _text(client.get("/"))
    assert "Nothing tracked yet" in body


def test_index_surfaces_closing_soon_summary(client):
    _add("Lilia", "place-lilia", closing_soon=True, summary="Last service in March")

    body = _text(client.get("/"))
    assert "Closing soon" in body
    assert "Last service in March" in body


def test_index_includes_archived_restaurants(client):
    """main.py archives permanent closures out of list_restaurants(), which is
    exactly why the dashboard asks for active_only=False -- an archived place
    dropping off the page entirely is indistinguishable from never having
    been tracked."""
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(restaurant_id)

    body = _text(client.get("/"))
    assert "Lilia" in body
    assert "Archived" in body


def test_index_sorts_concerns_above_healthy_rows(client):
    _add("Aaa Diner", "place-aaa")                                  # open, sorts by name
    _add("Zzz Tavern", "place-zzz", status="CLOSED_PERMANENTLY")    # worst news

    body = _text(client.get("/"))
    assert body.index("Zzz Tavern") < body.index("Aaa Diner")


def test_summary_counts_exclude_archived_from_tracked(client):
    _add("Lilia", "place-lilia")
    archived = _add("Gone", "place-gone", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(archived)

    body = _text(client.get("/"))
    # One tracked (Lilia), one archived -- the archived row must not inflate
    # the tracked count the page leads with.
    assert _stat(body, "Tracked") == 1
    assert _stat(body, "Archived") == 1


def test_detail_shows_history_rollups(client):
    restaurant_id = _add("Lilia", "place-lilia", closing_soon=True, summary="Closing in June")

    body = _text(client.get(f"/restaurant/{restaurant_id}"))
    assert "Closing in June" in body
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert month in body


def test_detail_404s_for_unknown_restaurant(client):
    assert client.get("/restaurant/999").status_code == 404


def test_detail_reachable_for_archived_restaurant(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(restaurant_id)

    assert client.get(f"/restaurant/{restaurant_id}").status_code == 200


def test_never_checked_restaurant_is_not_stale(client):
    """A freshly seeded restaurant hasn't missed a check, so it must not be
    counted among the ones whose scheduler looks dead."""
    _add("Lilia", "place-lilia")

    body = _text(client.get("/"))
    assert "never" in body
    assert "is the scheduler still running?" not in body


def test_old_last_check_is_flagged_stale(client):
    restaurant_id = _add("Lilia", "place-lilia")
    stale_at = datetime.now(timezone.utc) - timedelta(days=dashboard.STALE_AFTER_DAYS + 1)
    with db.get_conn() as conn:
        conn.execute("UPDATE restaurants SET last_checked_at = ? WHERE id = ?",
                     (stale_at.strftime("%Y-%m-%d %H:%M:%S"), restaurant_id))

    body = _text(client.get("/"))
    assert "Stale" in body
    assert "is the scheduler still running?" in body


def test_timestamps_are_read_as_utc_not_local(tmp_path, monkeypatch):
    """db writes CURRENT_TIMESTAMP, which is UTC. Parsing it as local time
    would skew every age on the page by the viewer's offset -- and on
    UTC-behind machines would put the last check in the future."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    restaurant_id = _add("Lilia", "place-lilia", status="OPERATIONAL")

    restaurant = db.get_restaurant(restaurant_id)
    parsed = dashboard._parse_ts(restaurant["last_checked_at"])
    assert parsed.tzinfo is not None
    # Just written, so its age is seconds -- not the hours a bad tz read gives.
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_parse_ts_tolerates_missing_and_malformed_values():
    assert dashboard._parse_ts(None) is None
    assert dashboard._parse_ts("") is None
    assert dashboard._parse_ts("not a date") is None


def test_age_labels_read_naturally():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    assert dashboard._age(now - timedelta(days=1), now) == "1 day ago"
    assert dashboard._age(now - timedelta(days=3), now) == "3 days ago"
    assert dashboard._age(now - timedelta(hours=5), now) == "5 hours ago"
    assert dashboard._age(now - timedelta(seconds=10), now) == "just now"
    assert dashboard._age(None, now) is None


# --- un-archiving ---------------------------------------------------------

def test_unarchive_puts_restaurant_back_on_active_list(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    db.archive_restaurant(restaurant_id)
    assert db.list_restaurants(active_only=True) == []

    response = _post_unarchive(client, restaurant_id)

    assert response.status_code == 302
    assert [r["id"] for r in db.list_restaurants(active_only=True)] == [restaurant_id]


def test_unarchive_leaves_status_and_flag_untouched(client):
    """Only `archived` may change. Writing an optimistic OPERATIONAL here
    would read as a status change on the next run and re-fire a closure alert
    that already went out."""
    restaurant_id = _add("Lilia", "place-lilia",
                         status="CLOSED_PERMANENTLY", closing_soon=True,
                         summary="Final service in March")
    db.archive_restaurant(restaurant_id)

    _post_unarchive(client, restaurant_id)

    restaurant = db.get_restaurant(restaurant_id)
    assert restaurant["archived"] == 0
    assert restaurant["business_status"] == "CLOSED_PERMANENTLY"
    assert restaurant["closing_soon_flag"] == 1
    assert restaurant["closing_soon_summary"] == "Final service in March"


def test_unarchive_warns_when_google_still_says_closed(client):
    """run_check archives on the status itself, so this row will be archived
    again on the next run -- the page has to say so rather than implying the
    re-activation stuck."""
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(restaurant_id)

    body = _text(_post_unarchive(client, restaurant_id, follow_redirects=True))
    assert "will archive it again" in body


def test_unarchive_of_temporary_closure_has_no_warning(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    db.archive_restaurant(restaurant_id)

    body = _text(_post_unarchive(client, restaurant_id, follow_redirects=True))
    assert "back on the active list" in body
    assert "will archive it again" not in body


def test_unarchive_is_idempotent_on_an_active_restaurant(client):
    """A double-submitted form should not claim to have changed something."""
    restaurant_id = _add("Lilia", "place-lilia")

    body = _text(_post_unarchive(client, restaurant_id, follow_redirects=True))
    assert "already active" in body
    assert db.get_restaurant(restaurant_id)["archived"] == 0


def test_unarchive_rejects_missing_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia")
    db.archive_restaurant(restaurant_id)

    response = client.post(f"/restaurant/{restaurant_id}/unarchive", data={})

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_rejects_wrong_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia")
    db.archive_restaurant(restaurant_id)

    response = _post_unarchive(client, restaurant_id, token="not-the-token")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_rejects_get(client):
    """A state change behind a GET is one crawler or prefetch away from
    firing on its own."""
    restaurant_id = _add("Lilia", "place-lilia")
    db.archive_restaurant(restaurant_id)

    assert client.get(f"/restaurant/{restaurant_id}/unarchive").status_code == 405
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_404s_for_unknown_restaurant(client):
    assert _post_unarchive(client, 999).status_code == 404


def test_unarchive_returns_to_the_page_it_came_from(client):
    restaurant_id = _add("Lilia", "place-lilia")
    db.archive_restaurant(restaurant_id)

    from_index = _post_unarchive(client, restaurant_id, return_to="index")
    assert from_index.headers["Location"] == "/"

    db.archive_restaurant(restaurant_id)
    from_detail = _post_unarchive(client, restaurant_id, return_to="detail")
    assert from_detail.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_return_to_cannot_redirect_off_site(client):
    """`return_to` picks between two endpoint names; anything else falls back
    to the detail page, so it cannot be used as an open redirect."""
    restaurant_id = _add("Lilia", "place-lilia")
    db.archive_restaurant(restaurant_id)

    response = _post_unarchive(client, restaurant_id,
                               return_to="https://evil.example.com/")

    assert response.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_reactivate_button_shown_only_for_archived_rows(client):
    active_id = _add("Lilia", "place-lilia")
    archived_id = _add("Gone", "place-gone", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(archived_id)

    body = _text(client.get("/"))
    assert f"/restaurant/{archived_id}/unarchive" in body
    assert f"/restaurant/{active_id}/unarchive" not in body


def test_detail_page_offers_reactivation_when_archived(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(restaurant_id)

    body = _text(client.get(f"/restaurant/{restaurant_id}"))
    assert "Re-activate" in body
    assert f"/restaurant/{restaurant_id}/unarchive" in body


def test_unarchive_accepts_a_token_from_a_rendered_page(client):
    """End to end through the markup: the token the page actually renders is
    the one the view accepts. The other tests seed the session directly, so
    this is what would catch the form and the check drifting apart."""
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    db.archive_restaurant(restaurant_id)

    page = _text(client.get("/"))
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    response = client.post(f"/restaurant/{restaurant_id}/unarchive",
                           data={"csrf_token": token})

    assert response.status_code == 302
    assert db.get_restaurant(restaurant_id)["archived"] == 0


def test_closing_soon_stat_matches_the_badges_on_screen(client):
    """A permanently-closed row keeps its old closing-soon flag, so the stat
    and the badge have to agree about whether that still counts as news --
    otherwise the page says "1 closing soon" with no such row visible. This
    is the state an un-archived permanent closure sits in.
    """
    restaurant_id = _add("Gone", "place-gone",
                         status="CLOSED_PERMANENTLY", closing_soon=True,
                         summary="Announced final service")
    assert db.get_restaurant(restaurant_id)["closing_soon_flag"] == 1

    body = _text(client.get("/"))
    assert _stat(body, "Closing soon") == 0
    assert 'badge warn">Closing soon' not in body
    # The summary itself still shows -- it is the context for the closure.
    assert "Announced final service" in body
