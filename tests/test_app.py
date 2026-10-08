"""Tests for the dashboard (app.py).

Most of the dashboard reads, so much of what can break is the *reading*:
timestamps stored as naive UTC being read as local time, the closing-soon
flag and status rendering out of step with what main.py notified on, or
archived rows vanishing from a page that exists to show them.

Un-archiving and adding are the writes, and they get the scrutiny a state
change deserves: that un-archiving flips only `archived`, that neither runs
without a valid CSRF token, that GET cannot trigger either, and that they're
honest about a row that was already there.

Adding gets a second kind of scrutiny, because the Places Text Search it runs
costs money and returns exactly one best guess. Several tests below assert
the search did *not* happen -- on an empty box, on a rejected token, on a GET
-- and that nothing is tracked until the separate confirm POST.

Same setup as the other tests: a real temp SQLite file via db.DB_PATH, so
the rows these pages render are written and read exactly as in production.
"""
import re
from datetime import UTC, datetime, timedelta

import pytest
from helpers import archive_restaurant

import app as dashboard
import checker
import db
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-key")
    db.init_db()
    return dashboard.create_app().test_client()


def _csrf(client):
    """Put a known CSRF token in the session and hand it back.

    The app mints tokens lazily, when a template actually renders a form, so
    what a page hands back depends on what was on it. Seeding the session
    keeps these tests about the POST behaviour rather than the markup;
    test_unarchive_accepts_a_token_from_a_rendered_page covers the real round
    trip through a page.
    """
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    return "test-csrf-token"


def _post_unarchive(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/unarchive", data=data,
                       follow_redirects=follow_redirects)


def _add(name, place_id, verified=True, **checks):
    """Seed one restaurant, optionally running a check result through it.

    Verified by default: an unverified row is one main.run_check refuses to
    check, and it renders with its own badge and a confirm prompt, so leaving
    every fixture unverified would put that state under tests about something
    else entirely. The verification tests pass verified=False.
    """
    db.add_restaurant(name, place_id, address=f"{name} address", verified=verified)
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


_UNSET = object()


def _place(place_id="place-lilia", name="Lilia",
           address="567 Union Ave, Brooklyn, NY", maps_url="https://maps.example/lilia"):
    """A Places Text Search hit, in the shape find_place_id() returns one."""
    return {"id": place_id, "displayName": {"text": name},
            "formattedAddress": address, "googleMapsUri": maps_url}


def _patch_search(monkeypatch, result=_UNSET):
    """Fake the Text Search. `result` is a place dict, None for "no match",
    or an Exception to raise.

    The returned call list is half the point: this call costs money, so
    several tests below are about it *not* happening.
    """
    calls = []

    def _fake(name, address_hint=""):
        calls.append((name, address_hint))
        if isinstance(result, Exception):
            raise result
        return _place() if result is _UNSET else result

    monkeypatch.setattr(dashboard, "find_place_id", _fake)
    return calls


def _post_add(client, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post("/add", data=data, follow_redirects=follow_redirects)


def _post_confirm(client, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post("/add/confirm", data=data, follow_redirects=follow_redirects)


def _tracked():
    return db.list_restaurants(active_only=False)


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
    archive_restaurant(restaurant_id)

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
    archive_restaurant(archived)

    body = _text(client.get("/"))
    # One tracked (Lilia), one archived -- the archived row must not inflate
    # the tracked count the page leads with.
    assert _stat(body, "Tracked") == 1
    assert _stat(body, "Archived") == 1


def test_detail_shows_history_rollups(client):
    restaurant_id = _add("Lilia", "place-lilia", closing_soon=True, summary="Closing in June")

    body = _text(client.get(f"/restaurant/{restaurant_id}"))
    assert "Closing in June" in body
    month = datetime.now(UTC).strftime("%Y-%m")
    assert month in body


def test_detail_404s_for_unknown_restaurant(client):
    assert client.get("/restaurant/999").status_code == 404


def test_detail_reachable_for_archived_restaurant(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    archive_restaurant(restaurant_id)

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
    stale_at = datetime.now(UTC) - timedelta(days=dashboard.STALE_AFTER_DAYS + 1)
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
    db.init_db()
    restaurant_id = _add("Lilia", "place-lilia", status="OPERATIONAL")

    restaurant = db.get_restaurant(restaurant_id)
    parsed = dashboard._parse_ts(restaurant["last_checked_at"])
    assert parsed.tzinfo is not None
    # Just written, so its age is seconds -- not the hours a bad tz read gives.
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


def test_parse_ts_tolerates_missing_and_malformed_values():
    assert dashboard._parse_ts(None) is None
    assert dashboard._parse_ts("") is None
    assert dashboard._parse_ts("not a date") is None


def test_age_labels_read_naturally():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    assert dashboard._age(now - timedelta(days=1), now) == "1 day ago"
    assert dashboard._age(now - timedelta(days=3), now) == "3 days ago"
    assert dashboard._age(now - timedelta(hours=5), now) == "5 hours ago"
    assert dashboard._age(now - timedelta(seconds=10), now) == "just now"
    assert dashboard._age(None, now) is None


# --- un-archiving ---------------------------------------------------------

def test_unarchive_puts_restaurant_back_on_active_list(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    archive_restaurant(restaurant_id)
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
    archive_restaurant(restaurant_id)

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
    archive_restaurant(restaurant_id)

    body = _text(_post_unarchive(client, restaurant_id, follow_redirects=True))
    assert "will archive it again" in body


def test_unarchive_of_temporary_closure_has_no_warning(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    archive_restaurant(restaurant_id)

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
    archive_restaurant(restaurant_id)

    response = client.post(f"/restaurant/{restaurant_id}/unarchive", data={})

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_rejects_wrong_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia")
    archive_restaurant(restaurant_id)

    response = _post_unarchive(client, restaurant_id, token="not-the-token")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_rejects_get(client):
    """A state change behind a GET is one crawler or prefetch away from
    firing on its own."""
    restaurant_id = _add("Lilia", "place-lilia")
    archive_restaurant(restaurant_id)

    assert client.get(f"/restaurant/{restaurant_id}/unarchive").status_code == 405
    assert db.get_restaurant(restaurant_id)["archived"] == 1


def test_unarchive_404s_for_unknown_restaurant(client):
    assert _post_unarchive(client, 999).status_code == 404


def test_unarchive_returns_to_the_page_it_came_from(client):
    restaurant_id = _add("Lilia", "place-lilia")
    archive_restaurant(restaurant_id)

    from_index = _post_unarchive(client, restaurant_id, return_to="index")
    assert from_index.headers["Location"] == "/"

    archive_restaurant(restaurant_id)
    from_detail = _post_unarchive(client, restaurant_id, return_to="detail")
    assert from_detail.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_return_to_cannot_redirect_off_site(client):
    """`return_to` picks between two endpoint names; anything else falls back
    to the detail page, so it cannot be used as an open redirect."""
    restaurant_id = _add("Lilia", "place-lilia")
    archive_restaurant(restaurant_id)

    response = _post_unarchive(client, restaurant_id,
                               return_to="https://evil.example.com/")

    assert response.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_reactivate_button_shown_only_for_archived_rows(client):
    active_id = _add("Lilia", "place-lilia")
    archived_id = _add("Gone", "place-gone", status="CLOSED_PERMANENTLY")
    archive_restaurant(archived_id)

    body = _text(client.get("/"))
    assert f"/restaurant/{archived_id}/unarchive" in body
    assert f"/restaurant/{active_id}/unarchive" not in body


def test_detail_page_offers_reactivation_when_archived(client):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    archive_restaurant(restaurant_id)

    body = _text(client.get(f"/restaurant/{restaurant_id}"))
    assert "Re-activate" in body
    assert f"/restaurant/{restaurant_id}/unarchive" in body


def test_unarchive_accepts_a_token_from_a_rendered_page(client):
    """End to end through the markup: the token the page actually renders is
    the one the view accepts. The other tests seed the session directly, so
    this is what would catch the form and the check drifting apart."""
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    archive_restaurant(restaurant_id)

    page = _text(client.get("/"))
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    response = client.post(f"/restaurant/{restaurant_id}/unarchive",
                           data={"csrf_token": token})

    assert response.status_code == 302
    assert db.get_restaurant(restaurant_id)["archived"] == 0


# --- adding a restaurant --------------------------------------------------

def test_add_search_confirms_before_tracking_anything(client, monkeypatch):
    """The search spends money; it must not also spend a row. Text Search
    returns one best guess and no confidence with it, so the guess gets shown
    and agreed to before anything is watched."""
    calls = _patch_search(monkeypatch)

    response = _post_add(client, name="Lilia", address_hint="Brooklyn")

    assert calls == [("Lilia", "Brooklyn")]
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/add/confirm")
    assert _tracked() == []  # searched, not added

    body = _text(client.get("/add/confirm"))
    assert "Lilia" in body
    assert "567 Union Ave, Brooklyn, NY" in body
    assert "https://maps.example/lilia" in body


def test_confirm_tracks_the_restaurant(client, monkeypatch):
    _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    response = _post_confirm(client, place_id="place-lilia")

    rows = _tracked()
    assert len(rows) == 1
    assert rows[0]["name"] == "Lilia"
    assert rows[0]["address"] == "567 Union Ave, Brooklyn, NY"
    assert rows[0]["maps_url"] == "https://maps.example/lilia"
    assert rows[0]["business_status"] == "OPERATIONAL"
    assert rows[0]["last_checked_at"] is None  # nothing has checked it yet
    assert response.headers["Location"].endswith(f"/restaurant/{rows[0]['id']}")


def test_confirm_stores_googles_name_not_the_typed_one(client, monkeypatch):
    """Same rule seed.py follows -- the stored name is Google's canonical one,
    so the page and the notifications don't end up saying "lilia bklyn"."""
    _patch_search(monkeypatch)
    _post_add(client, name="lilia bklyn")

    _post_confirm(client, place_id="place-lilia")

    assert _tracked()[0]["name"] == "Lilia"


def test_confirm_page_reload_does_not_pay_for_another_search(client, monkeypatch):
    """Why the search redirects instead of rendering the result itself: a
    reload of a POSTed page re-runs the POST, and this POST has a bill."""
    calls = _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    _text(client.get("/add/confirm"))
    _text(client.get("/add/confirm"))

    assert len(calls) == 1


def test_add_search_without_a_name_does_not_call_places(client, monkeypatch):
    """An empty box is a slip; finding that out shouldn't cost a search."""
    calls = _patch_search(monkeypatch)

    body = _text(_post_add(client, name="   ", follow_redirects=True))

    assert calls == []
    assert "Enter a restaurant name" in body
    assert _tracked() == []


def test_add_search_reports_no_match(client, monkeypatch):
    _patch_search(monkeypatch, result=None)

    body = _text(_post_add(client, name="Not A Real Place", follow_redirects=True))

    assert "No match" in body
    assert _tracked() == []


def test_add_search_survives_a_places_failure(client, monkeypatch):
    """A missing API key or a dead Places API is a message, not a 500 -- the
    dashboard is where someone would find out the key was never set."""
    _patch_search(monkeypatch, result=RuntimeError("Set GOOGLE_PLACES_API_KEY"))

    body = _text(_post_add(client, name="Lilia", follow_redirects=True))

    assert "reach Google Places" in body  # apostrophe is HTML-escaped in the flash
    assert _tracked() == []


def test_add_search_of_a_tracked_place_goes_to_the_existing_row(client, monkeypatch):
    """add_restaurant() dedupes on place_id by quietly ignoring the insert,
    which would read as "added" on a page that then shows a single row. Say
    which row it is instead."""
    existing_id = _add("Lilia", "place-lilia")
    _patch_search(monkeypatch)

    response = _post_add(client, name="Lilia")

    assert response.headers["Location"].endswith(f"/restaurant/{existing_id}")
    assert "Already tracking Lilia" in _text(client.get(f"/restaurant/{existing_id}"))
    assert len(_tracked()) == 1


def test_add_search_of_an_archived_place_says_it_is_archived(client, monkeypatch):
    """Searching for something you'd given up on is how you would rediscover
    that it reopened -- and the row it lands on has the Re-activate button."""
    existing_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    archive_restaurant(existing_id)
    _patch_search(monkeypatch)

    body = _text(_post_add(client, name="Lilia", follow_redirects=True))

    assert "archived" in body
    assert "Re-activate" in body
    assert len(_tracked()) == 1


def test_add_search_rejects_missing_csrf_token(client, monkeypatch):
    """No token, no search -- the rejection has to land before the money."""
    calls = _patch_search(monkeypatch)

    response = client.post("/add", data={"name": "Lilia"})

    assert response.status_code == 400
    assert calls == []
    assert _tracked() == []


def test_add_search_rejects_wrong_csrf_token(client, monkeypatch):
    calls = _patch_search(monkeypatch)

    response = _post_add(client, token="not-the-token", name="Lilia")

    assert response.status_code == 400
    assert calls == []


def test_add_search_rejects_get(client, monkeypatch):
    """A paid call behind a GET is one prefetch or crawler away from running
    on its own, repeatedly."""
    calls = _patch_search(monkeypatch)

    assert client.get("/add?name=Lilia").status_code == 405
    assert calls == []


def test_confirm_rejects_missing_csrf_token(client, monkeypatch):
    _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    response = client.post("/add/confirm", data={"place_id": "place-lilia"})

    assert response.status_code == 400
    assert _tracked() == []


def test_confirm_page_shows_without_committing(client, monkeypatch):
    """GET /add/confirm renders the candidate; only the POST may commit it."""
    _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    _text(client.get("/add/confirm"))

    assert _tracked() == []


def test_confirm_without_a_pending_search_expires(client):
    """A bookmarked URL, or a restart that invalidated the session cookie."""
    body = _text(_post_confirm(client, place_id="place-lilia", follow_redirects=True))

    assert "expired" in body
    assert _tracked() == []


def test_confirm_page_without_a_pending_search_expires(client):
    body = _text(client.get("/add/confirm", follow_redirects=True))

    assert "expired" in body


def test_confirm_rejects_a_place_id_the_search_did_not_return(client, monkeypatch):
    """The row is built from the session copy, so a doctored form can't put
    one restaurant on the page and file a different one -- but it shouldn't
    quietly track the shown one either. Refuse the mismatch."""
    _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    response = _post_confirm(client, place_id="place-somewhere-else")

    assert response.status_code == 400
    assert _tracked() == []


def test_confirm_twice_does_not_duplicate_or_claim_a_second_add(client, monkeypatch):
    _patch_search(monkeypatch)
    _post_add(client, name="Lilia")

    _post_confirm(client, place_id="place-lilia")
    body = _text(_post_confirm(client, place_id="place-lilia", follow_redirects=True))

    assert len(_tracked()) == 1
    assert "already being tracked" in body


def test_index_offers_the_search_form(client):
    body = _text(client.get("/"))

    assert 'action="/add"' in body
    assert 'name="address_hint"' in body
    # The page has to say what the button costs before it gets pressed.
    assert "one paid search per click" in body


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


# --- verifying a restaurant ---------------------------------------------

def _post_verify(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/verify", data=data,
                       follow_redirects=follow_redirects)


def _post_reject(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/reject", data=data,
                       follow_redirects=follow_redirects)


def test_unverified_restaurant_is_listed_for_verification(client):
    _add("Lilia", "place-lilia", verified=False)

    body = client.get("/").get_data(as_text=True)

    assert "waiting to be verified" in body
    assert "Needs verifying" in body
    assert "Yes, that's the place" in body


def test_verified_restaurant_is_not_listed_for_verification(client):
    _add("Lilia", "place-lilia")

    body = client.get("/").get_data(as_text=True)

    assert "waiting to be verified" not in body
    assert "Needs verifying" not in body


def test_never_checked_restaurant_is_not_reported_as_open(client):
    """business_status defaults to OPERATIONAL in the schema, so a row nobody
    has looked up would otherwise wear an "Open" badge on a default -- and
    wear it forever now, since an unverified row is never checked."""
    _add("Lilia", "place-lilia", verified=False)

    body = client.get("/").get_data(as_text=True)

    assert ">Open<" not in body


def test_verify_marks_the_row_and_nothing_else(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    before = db.get_restaurant(restaurant_id)

    _post_verify(client, restaurant_id)

    after = db.get_restaurant(restaurant_id)
    assert after["verified_at"] is not None
    assert {k: v for k, v in after.items() if k != "verified_at"} == \
           {k: v for k, v in before.items() if k != "verified_at"}


def test_verify_requires_a_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = _post_verify(client, restaurant_id, token="wrong")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["verified_at"] is None


def test_verify_is_unreachable_by_get(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = client.get(f"/restaurant/{restaurant_id}/verify")

    assert response.status_code == 405
    assert db.get_restaurant(restaurant_id)["verified_at"] is None


def test_verify_reports_a_repeat_rather_than_a_change(client):
    """Double-submitted form: the timestamp must not move, and the page must
    not claim something just happened."""
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    _post_verify(client, restaurant_id)
    first = db.get_restaurant(restaurant_id)["verified_at"]

    body = _post_verify(client, restaurant_id,
                        follow_redirects=True).get_data(as_text=True)

    assert db.get_restaurant(restaurant_id)["verified_at"] == first
    assert "was already verified" in body


def test_verify_404s_for_an_unknown_restaurant(client):
    assert _post_verify(client, 999).status_code == 404


def _post_verify_selected(client, ids, token=None, follow_redirects=False):
    data = {"csrf_token": _csrf(client) if token is None else token, "ids": ids}
    return client.post("/verify-selected", data=data,
                       follow_redirects=follow_redirects)


def test_bulk_verify_verifies_only_the_selected_rows(client):
    a = _add("Lilia", "place-lilia", verified=False)
    b = _add("Carbone", "place-carbone", verified=False)
    c = _add("Via Carota", "place-carota", verified=False)

    body = _post_verify_selected(client, [a, c],
                                 follow_redirects=True).get_data(as_text=True)

    assert db.get_restaurant(a)["verified_at"] is not None
    assert db.get_restaurant(b)["verified_at"] is None
    assert db.get_restaurant(c)["verified_at"] is not None
    assert "Verified 2 restaurants" in body


def test_bulk_verify_touches_only_verified_at(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    before = db.get_restaurant(restaurant_id)

    _post_verify_selected(client, [restaurant_id])

    after = db.get_restaurant(restaurant_id)
    assert {k: v for k, v in after.items() if k != "verified_at"} == \
           {k: v for k, v in before.items() if k != "verified_at"}


def test_bulk_verify_counts_only_rows_that_changed(client):
    fresh = _add("Lilia", "place-lilia", verified=False)
    done = _add("Carbone", "place-carbone", verified=True)
    stamp = db.get_restaurant(done)["verified_at"]

    body = _post_verify_selected(client, [fresh, done, 999],
                                 follow_redirects=True).get_data(as_text=True)

    assert "Verified 1 restaurant " in body
    assert db.get_restaurant(done)["verified_at"] == stamp


def test_bulk_verify_with_nothing_selected_changes_nothing(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    body = client.post("/verify-selected", data={"csrf_token": _csrf(client)},
                       follow_redirects=True).get_data(as_text=True)

    assert "Tick at least one" in body
    assert db.get_restaurant(restaurant_id)["verified_at"] is None


def test_bulk_verify_ignores_malformed_ids(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = _post_verify_selected(client, ["abc", str(restaurant_id), ""])

    assert response.status_code == 302
    assert db.get_restaurant(restaurant_id)["verified_at"] is not None


def test_bulk_verify_requires_a_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = _post_verify_selected(client, [restaurant_id], token="wrong")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id)["verified_at"] is None


def test_bulk_verify_is_unreachable_by_get(client):
    assert client.get("/verify-selected").status_code == 405


def test_index_offers_a_checkbox_per_unverified_row_and_none_otherwise(client):
    pending = _add("Lilia", "place-lilia", verified=False)
    _add("Carbone", "place-carbone", verified=True)

    body = client.get("/").get_data(as_text=True)

    assert body.count('name="ids"') == 1
    assert f'value="{pending}"' in body
    assert "Verify selected" in body


def test_reject_deletes_the_restaurant_and_its_history(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    db.update_check_result(restaurant_id, "OPERATIONAL", False, "")

    _post_reject(client, restaurant_id)

    assert db.get_restaurant(restaurant_id) is None
    assert db.check_history(restaurant_id) == []


def test_reject_reopens_the_search_with_the_name_filled_in(client):
    """The address isn't carried over on purpose -- it belongs to the wrong
    place, so re-searching with it would just find that place again."""
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = _post_reject(client, restaurant_id)
    assert response.headers["Location"].endswith("/?q=Lilia")

    body = client.get(response.headers["Location"]).get_data(as_text=True)
    assert 'value="Lilia"' in body
    assert "Lilia address" not in body


def test_reject_flash_renders_its_em_dash(client):
    """The message spent a while reading "Removed Lilia. Search again with a
    street or city \\u2014 that's what narrows it down." on screen: the escape
    was doubled in the source, so Python stored the six characters instead of
    the dash. Nothing asserted on this string, which is exactly why it
    survived -- the other reject tests check the redirect and the row, not the
    words. Asserting the backslash is *absent* is the part that catches a
    re-doubling; the dash alone would pass either way on a sloppy match."""
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    body = _text(_post_reject(client, restaurant_id, follow_redirects=True))

    assert "street or city — that" in body
    assert "u2014" not in body


def test_reject_requires_a_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    response = _post_reject(client, restaurant_id, token="wrong")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id) is not None


def test_reject_is_unreachable_by_get(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)

    assert client.get(f"/restaurant/{restaurant_id}/reject").status_code == 405
    assert db.get_restaurant(restaurant_id) is not None


def test_reject_refuses_to_delete_a_verified_restaurant(client):
    """This is the only route that destroys data, so it's scoped to rows
    nobody has vouched for yet: a stale tab must not be able to throw away a
    restaurant confirmed long ago, history and all."""
    restaurant_id = _add("Lilia", "place-lilia")

    response = _post_reject(client, restaurant_id)

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id) is not None


def test_dashboard_adds_are_stored_already_verified(client):
    """The confirm page is the verification. Asking again at first check
    would be the same question about the same two facts -- and would leave a
    just-added restaurant unchecked until it was answered twice."""
    token = _csrf(client)
    db.stash_pending_add("tok", {"name": "Lilia", "place_id": "place-lilia",
                                 "address": "567 Union Ave", "maps_url": "",
                                 "query": "lilia"})
    with client.session_transaction() as sess:
        sess["pending_add"] = "tok"

    client.post("/add/confirm", data={"csrf_token": token, "place_id": "place-lilia"})

    assert db.get_restaurant_by_place_id("place-lilia")["verified_at"] is not None


def test_verify_button_round_trips_a_token_from_the_rendered_page(client):
    """The other verification tests seed the session, so nothing else would
    notice the form and the check disagreeing about the field name."""
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    body = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    client.post(f"/restaurant/{restaurant_id}/verify", data={"csrf_token": token})

    assert db.get_restaurant(restaurant_id)["verified_at"] is not None


def test_unverified_count_matches_the_rows_on_the_page(client):
    """Same guard as the closing-soon count: the summary tile and the list
    are computed from one pass, and this is what stops them drifting."""
    _add("Lilia", "place-lilia", verified=False)
    _add("Don Angie", "place-don-angie", verified=False)
    _add("Katz's", "place-katz")

    body = client.get("/").get_data(as_text=True)

    assert "<b>2</b><span>To verify</span>" in body
    assert body.count("Needs verifying") == 2
# --- deleting a restaurant ----------------------------------------------

def _post_delete(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/delete", data=data,
                       follow_redirects=follow_redirects)


def test_every_row_offers_a_delete_button(client):
    """Including the archived one: archiving is what the checker does on a
    closure, so "I don't want this watched" is still unanswered for those
    rows -- and they're the likeliest thing anyone wants gone."""
    active_id = _add("Lilia", "place-lilia")
    archived_id = _add("Gone", "place-gone", status="CLOSED_PERMANENTLY")
    archive_restaurant(archived_id)

    body = client.get("/").get_data(as_text=True)

    assert f"/restaurant/{active_id}/delete" in body
    assert f"/restaurant/{archived_id}/delete" in body
    assert body.count(">Delete<") == 2


def test_delete_button_asks_for_confirmation(client):
    _add("Lilia", "place-lilia")

    body = client.get("/").get_data(as_text=True)

    assert "data-confirm-delete=" in body
    js = client.get("/static/app.js").get_data(as_text=True)
    assert "confirm(" in js
    assert "cannot be undone" in js


def test_delete_confirmation_survives_an_apostrophe_in_the_name(client):
    """The name rides in a data attribute that app.js reads back as text, so
    there is no JavaScript string for a quote to end early. Autoescaping has
    to keep it from ending the *attribute* instead."""
    _add("Katz's Delicatessen", "place-katz")
    _add('Say "Hi" <b>', "place-hi")

    body = client.get("/").get_data(as_text=True)

    assert 'data-confirm-delete="Katz&#39;s Delicatessen"' in body
    assert 'data-confirm-delete="Say &#34;Hi&#34; &lt;b&gt;"' in body


def test_delete_removes_the_restaurant_and_its_history(client):
    restaurant_id = _add("Lilia", "place-lilia")
    db.update_check_result(restaurant_id, "OPERATIONAL", False, "")

    _post_delete(client, restaurant_id)

    assert db.get_restaurant(restaurant_id) is None
    assert db.check_history(restaurant_id) == []


def test_delete_leaves_the_other_restaurants_alone(client):
    doomed = _add("Lilia", "place-lilia")
    kept = _add("Don Angie", "place-don-angie")

    _post_delete(client, doomed)

    assert [r["id"] for r in db.list_restaurants(active_only=False)] == [kept]


def test_delete_works_on_a_verified_restaurant(client):
    """Unlike /reject, which guards a confirmed row against a stale tab. Here
    deleting one is the entire point, and the dialog is the guard."""
    restaurant_id = _add("Lilia", "place-lilia")
    assert db.get_restaurant(restaurant_id)["verified_at"] is not None

    response = _post_delete(client, restaurant_id)

    assert response.status_code == 302
    assert db.get_restaurant(restaurant_id) is None


def test_delete_returns_to_the_list_and_says_what_went(client):
    restaurant_id = _add("Lilia", "place-lilia")

    response = _post_delete(client, restaurant_id)
    assert response.headers["Location"] == "/"

    body = _text(client.get("/"))
    assert "Removed Lilia" in body
    assert "Lilia" not in body.split("<tbody>")[1]


def test_delete_requires_a_csrf_token(client):
    restaurant_id = _add("Lilia", "place-lilia")

    response = _post_delete(client, restaurant_id, token="wrong")

    assert response.status_code == 400
    assert db.get_restaurant(restaurant_id) is not None


def test_delete_is_unreachable_by_get(client):
    restaurant_id = _add("Lilia", "place-lilia")

    assert client.get(f"/restaurant/{restaurant_id}/delete").status_code == 405
    assert db.get_restaurant(restaurant_id) is not None


def test_delete_of_an_already_deleted_restaurant_is_not_an_error(client):
    """A double-clicked button. The row is gone either way, which is what was
    asked for, and a 404 page in place of the list would look like a bug."""
    restaurant_id = _add("Lilia", "place-lilia")
    _post_delete(client, restaurant_id)

    body = _text(_post_delete(client, restaurant_id, follow_redirects=True))

    assert "had already been removed" in body


def test_delete_button_round_trips_a_token_from_the_rendered_page(client):
    """The POSTs above seed the session, so nothing else here would notice the
    form and the check disagreeing about the field name."""
    restaurant_id = _add("Lilia", "place-lilia")
    body = client.get("/").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)

    client.post(f"/restaurant/{restaurant_id}/delete", data={"csrf_token": token})

    assert db.get_restaurant(restaurant_id) is None


# --- checking a restaurant now -------------------------------------------
#
# The button runs checker.check_one, the same function the weekly run calls, so
# these tests are mostly about the two things the route adds around it: that
# it is offered and honoured for exactly the rows a scheduled run would check,
# and that it stays on the cheap half. Several assert the news check didn't
# happen -- that call is allowed 120 seconds and costs real credits, and a
# request thread is the wrong place for both.


def _post_check(client, restaurant_id, token=None, follow_redirects=False, **fields):
    data = {"csrf_token": _csrf(client) if token is None else token, **fields}
    return client.post(f"/restaurant/{restaurant_id}/check", data=data,
                       follow_redirects=follow_redirects)


def _patch_check(monkeypatch, status="OPERATIONAL"):
    """Fake everything check_one reaches for, and record it.

    Patched on `checker`, not on `app`: the dashboard imports check_one, and that
    function resolves these names in checker's globals. Patching the wrong module
    would leave the real Places call in place, which is the failure mode worth
    being explicit about.

    `status` is the businessStatus to answer with, or an Exception to raise.
    Returns (places_calls, news_calls, notifications) -- the news list is the
    one most of these tests assert is empty.
    """
    places, news, notifications = [], [], []

    def _fake_status(place_id):
        places.append(place_id)
        if isinstance(status, Exception):
            raise status
        return status

    def _fake_news(name, address=None):
        # Recorded rather than raised: check_one catches whatever a news check
        # throws, so an AssertionError in here would be swallowed and the test
        # would pass while the call was happening.
        news.append(name)
        return {"closing_soon": True, "confidence": "high", "summary": "from the news"}

    monkeypatch.setattr(checker, "get_business_status", _fake_status)
    monkeypatch.setattr(checker, "check_closing_soon", _fake_news)
    monkeypatch.setattr(checker, "notify_closed",
                        lambda r, s: notifications.append(("closed", r["name"], s)))
    monkeypatch.setattr(checker, "notify_closing_soon",
                        lambda r, summary: notifications.append(("soon", r["name"])))
    return places, news, notifications


def test_check_now_polls_places_and_stores_the_result(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    places, news, _ = _patch_check(monkeypatch, "CLOSED_TEMPORARILY")

    response = _post_check(client, restaurant_id)

    assert response.status_code == 302
    assert places == ["place-lilia"]
    assert news == []
    assert db.get_restaurant(restaurant_id)["business_status"] == "CLOSED_TEMPORARILY"


def test_check_now_appends_to_the_history(client, monkeypatch):
    """A manual check is a check: it belongs in the log the detail page reads,
    not off to one side, or the history would quietly disagree with the row."""
    restaurant_id = _add("Lilia", "place-lilia")
    _patch_check(monkeypatch)

    _post_check(client, restaurant_id)

    assert len(db.check_history(restaurant_id)) == 1


def test_check_now_never_runs_the_news_check(client, monkeypatch):
    """The whole scoping decision, in one assertion."""
    restaurant_id = _add("Lilia", "place-lilia")
    _, news, _ = _patch_check(monkeypatch)

    _post_check(client, restaurant_id)

    assert news == []


def test_check_now_carries_the_closing_soon_flag_forward(client, monkeypatch):
    """Only a news check can move that flag, and this isn't one. Clearing it
    here would re-arm an alert that has already been sent, so the next weekly
    news check would report the same closure story a second time."""
    restaurant_id = _add("Lilia", "place-lilia", closing_soon=True,
                         summary="Reported closing in March")
    _patch_check(monkeypatch)

    _post_check(client, restaurant_id)

    row = db.get_restaurant(restaurant_id)
    assert row["closing_soon_flag"] == 1
    assert row["closing_soon_summary"] == "Reported closing in March"


def test_check_now_notifies_on_a_move_into_closed(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    _, _, notifications = _patch_check(monkeypatch, "CLOSED_PERMANENTLY")

    _post_check(client, restaurant_id)

    assert notifications == [("closed", "Lilia", "CLOSED_PERMANENTLY")]


def test_check_now_does_not_re_notify_when_nothing_changed(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    _, _, notifications = _patch_check(monkeypatch, "CLOSED_TEMPORARILY")

    _post_check(client, restaurant_id)

    assert notifications == []


def test_check_now_archives_a_permanent_closure_and_says_so(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    _patch_check(monkeypatch, "CLOSED_PERMANENTLY")

    body = _text(_post_check(client, restaurant_id, follow_redirects=True))

    assert db.get_restaurant(restaurant_id)["archived"] == 1
    assert "been archived" in body


def test_check_now_settles_the_flag_on_a_permanent_closure(client, monkeypatch):
    """Same rule as a scheduled run: the closure landing settles the
    prediction, and this is the last check that can -- the row archives here,
    and the news check that owns the flag only runs on OPERATIONAL places."""
    restaurant_id = _add("Lilia", "place-lilia", closing_soon=True, summary="Reported")
    _patch_check(monkeypatch, "CLOSED_PERMANENTLY")

    _post_check(client, restaurant_id)

    row = db.get_restaurant(restaurant_id)
    assert row["closing_soon_flag"] == 0
    assert row["closing_soon_summary"] == "Reported"


def test_check_now_reports_no_change_plainly(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia", status="OPERATIONAL")
    _patch_check(monkeypatch, "OPERATIONAL")

    body = _text(_post_check(client, restaurant_id, follow_redirects=True))

    assert "still Open" in body


def test_check_now_reports_a_reopening(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_TEMPORARILY")
    _patch_check(monkeypatch, "OPERATIONAL")

    body = _text(_post_check(client, restaurant_id, follow_redirects=True))

    assert "is Open again" in body


def test_check_now_leaves_the_run_counter_alone(client, monkeypatch):
    """The counter decides when the next news check falls due for the whole
    list. A button on one restaurant must not move that schedule for the
    other fifty."""
    restaurant_id = _add("Lilia", "place-lilia")
    _patch_check(monkeypatch)
    counted = []
    monkeypatch.setattr(main, "_write_run_count", lambda n: counted.append(n))

    _post_check(client, restaurant_id)

    assert counted == []


def test_check_now_survives_a_dead_places_api(client, monkeypatch):
    """Nothing is written before the fetch returns, so there is no
    half-applied check to explain -- the row should read exactly as it did."""
    restaurant_id = _add("Lilia", "place-lilia", status="OPERATIONAL")
    before = db.check_history(restaurant_id)
    _patch_check(monkeypatch, RuntimeError("places is down"))

    body = _text(_post_check(client, restaurant_id, follow_redirects=True))

    assert "Couldn" in body and "reach Google Places" in body
    assert db.get_restaurant(restaurant_id)["business_status"] == "OPERATIONAL"
    # Not "empty" -- the fixture's own check wrote a row. Nothing was *added*.
    assert db.check_history(restaurant_id) == before


def test_check_now_requires_a_csrf_token(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    places, _, _ = _patch_check(monkeypatch)

    response = _post_check(client, restaurant_id, token="wrong")

    assert response.status_code == 400
    assert places == []


def test_check_now_is_unreachable_by_get(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    places, _, _ = _patch_check(monkeypatch)

    assert client.get(f"/restaurant/{restaurant_id}/check").status_code == 405
    assert places == []


def test_check_now_refuses_an_unverified_restaurant(client, monkeypatch):
    """run_check skips these entirely, on purpose -- nothing is spent on a row
    nobody has confirmed points at the right place. The button would be a way
    around that gate, so the route closes it."""
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    places, _, _ = _patch_check(monkeypatch)

    response = _post_check(client, restaurant_id)

    assert response.status_code == 400
    assert places == []


def test_check_now_refuses_an_archived_restaurant(client, monkeypatch):
    """Re-activating is how you resume checking one. Checking it in place
    would only archive it again on the same status."""
    restaurant_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    archive_restaurant(restaurant_id)
    places, _, _ = _patch_check(monkeypatch)

    response = _post_check(client, restaurant_id)

    assert response.status_code == 400
    assert places == []


def test_check_now_on_a_missing_restaurant_is_404(client, monkeypatch):
    places, _, _ = _patch_check(monkeypatch)

    assert _post_check(client, 9999).status_code == 404
    assert places == []


def test_check_button_is_offered_only_where_the_post_would_be_honoured(client):
    """The template reads `checkable` and the route reads `_is_checkable` --
    one rule, so the page cannot show a button that answers 400."""
    active = _add("Lilia", "place-lilia")
    unverified = _add("Unsure", "place-unsure", verified=False)
    archived = _add("Gone", "place-gone", status="CLOSED_PERMANENTLY")
    archive_restaurant(archived)

    body = client.get("/").get_data(as_text=True)

    assert f"/restaurant/{active}/check" in body
    assert f"/restaurant/{unverified}/check" not in body
    assert f"/restaurant/{archived}/check" not in body


def test_check_button_says_it_skips_the_news_check(client):
    """Check now, sitting next to a Closing soon badge, otherwise reads as a
    promise to go and re-read the news."""
    _add("Lilia", "place-lilia")

    body = client.get("/").get_data(as_text=True)

    assert "closing-soon news check" in body


def test_check_now_returns_to_the_page_it_was_pressed_on(client, monkeypatch):
    restaurant_id = _add("Lilia", "place-lilia")
    _patch_check(monkeypatch)

    from_index = _post_check(client, restaurant_id, return_to="index")
    from_detail = _post_check(client, restaurant_id)

    assert from_index.headers["Location"] == "/"
    assert from_detail.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_check_now_cannot_be_steered_off_site(client, monkeypatch):
    """`return_to` names an endpoint, never a URL."""
    restaurant_id = _add("Lilia", "place-lilia")
    _patch_check(monkeypatch)

    response = _post_check(client, restaurant_id, return_to="https://evil.example/")

    assert response.headers["Location"] == f"/restaurant/{restaurant_id}"


def test_check_button_round_trips_a_token_from_the_rendered_page(client, monkeypatch):
    """The POSTs above seed the session, so nothing else here would notice the
    form and the check disagreeing about the field name."""
    restaurant_id = _add("Lilia", "place-lilia")
    places, _, _ = _patch_check(monkeypatch)
    body = client.get("/").get_data(as_text=True)
    form = body.split(f"/restaurant/{restaurant_id}/check")[1]
    token = re.search(r'name="csrf_token" value="([^"]+)"', form).group(1)

    client.post(f"/restaurant/{restaurant_id}/check", data={"csrf_token": token})

    assert places == ["place-lilia"]


def test_logging_is_configured_when_nothing_has(monkeypatch):
    """`flask run` leaves the root logger without a handler, which drops
    every logger.info() in this module -- including the secret-key warning."""
    import logging

    import config
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))

    config.configure_logging()

    assert calls and calls[0]["level"] == logging.INFO


def test_existing_logging_is_left_alone(monkeypatch):
    import logging

    import config
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.NullHandler()])
    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))

    config.configure_logging()

    assert calls == []


# --- hardening, paging, and the stash --------------------------------------

def test_every_post_route_rejects_a_missing_token(client):
    """The check is one before_request hook, so a route added later is
    covered without remembering to ask. This walks the URL map rather than a
    list of routes someone has to keep up to date."""
    app = client.application
    posts = [r for r in app.url_map.iter_rules() if "POST" in r.methods]
    assert posts
    for rule in posts:
        url = rule.rule.replace("<int:restaurant_id>", "1")
        assert client.post(url, data={}).status_code == 400, url


def test_pages_carry_a_csp_and_no_inline_script_or_style(client):
    _add("Lilia", "place-lilia", verified=False)
    _add("Katz", "place-katz")

    for url in ("/", "/restaurant/1"):
        response = client.get(url)
        body = response.get_data(as_text=True)
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert "<style" not in body
        assert " style=" not in body
        assert "onsubmit=" not in body
        assert not re.search(r"<script(?![^>]*\bsrc=)", body)


def test_static_assets_are_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_verify_selected_ignores_out_of_range_ids(client):
    restaurant_id = _add("Lilia", "place-lilia", verified=False)
    token = _csrf(client)

    response = client.post("/verify-selected", data={
        "csrf_token": token, "ids": [str(10**30), "-4", "0", str(restaurant_id)]})

    assert response.status_code == 302
    assert db.get_restaurant(restaurant_id)["verified_at"] is not None


def test_the_index_is_paged_and_filterable(client, monkeypatch):
    monkeypatch.setattr(dashboard, "PAGE_SIZE", 3)
    for i in range(7):
        _add(f"Place {i}", f"place-{i}")
    _add("Katz", "place-katz")

    first = client.get("/").get_data(as_text=True)
    assert "Page 1 of 3" in first
    assert first.count(">Delete<") == 3
    # The summary still counts everything.
    assert _stat(first, "Tracked") == 8

    last = client.get("/?page=99").get_data(as_text=True)
    assert "Page 3 of 3" in last
    assert client.get("/?page=banana").status_code == 200

    found = client.get("/?find=katz").get_data(as_text=True)
    assert found.count(">Delete<") == 1
    assert "1 match" in found
    assert "No restaurant matches" in client.get("/?find=zzz").get_data(as_text=True)


def test_the_verify_card_is_capped(client, monkeypatch):
    monkeypatch.setattr(dashboard, "PENDING_SHOWN", 2)
    for i in range(5):
        _add(f"Place {i}", f"place-{i}", verified=False)

    body = client.get("/").get_data(as_text=True)

    assert body.count('class="pick"') == 2
    assert "Showing the first 2 of" in body
    assert _stat(body, "To verify") == 5


def test_an_expired_stash_is_not_confirmable(client):
    token = _csrf(client)
    db.stash_pending_add("old", {"name": "Lilia", "place_id": "place-lilia",
                                 "address": "", "maps_url": "", "query": "lilia"})
    with db.get_conn() as conn:
        conn.execute("UPDATE pending_adds SET created_at = datetime('now', '-3 hours')")
    with client.session_transaction() as sess:
        sess["pending_add"] = "old"

    client.post("/add/confirm", data={"csrf_token": token, "place_id": "place-lilia"})

    assert db.get_restaurant_by_place_id("place-lilia") is None


def test_create_app_takes_config_overrides(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    app = dashboard.create_app({"TESTING": True, "MAX_CONTENT_LENGTH": 1234})
    assert app.config["TESTING"] is True
    assert app.config["MAX_CONTENT_LENGTH"] == 1234


def test_check_now_reports_an_alert_that_could_not_be_sent(client, monkeypatch):
    """The status is already recorded by then, so the flash must not claim
    that nothing changed."""
    restaurant_id = _add("Lilia", "place-lilia")
    token = _csrf(client)

    def _fail(restaurant, include_news_check=False):
        raise checker.AlertNotSent("ntfy down")

    monkeypatch.setattr(dashboard, "check_one", _fail)

    response = client.post(f"/restaurant/{restaurant_id}/check",
                           data={"csrf_token": token}, follow_redirects=True)

    body = response.get_data(as_text=True)
    assert "alert couldn&#39;t be sent" in body
    assert "nothing was changed" not in body
