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
    assert response.headers["Location"].endswith("/restaurant/%d" % rows[0]["id"])


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

    assert response.headers["Location"].endswith("/restaurant/%d" % existing_id)
    assert "Already tracking Lilia" in _text(client.get("/restaurant/%d" % existing_id))
    assert len(_tracked()) == 1


def test_add_search_of_an_archived_place_says_it_is_archived(client, monkeypatch):
    """Searching for something you'd given up on is how you would rediscover
    that it reopened -- and the row it lands on has the Re-activate button."""
    existing_id = _add("Lilia", "place-lilia", status="CLOSED_PERMANENTLY")
    db.archive_restaurant(existing_id)
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
    with client.session_transaction() as sess:
        sess["pending_add"] = {"name": "Lilia", "place_id": "place-lilia",
                               "address": "567 Union Ave", "maps_url": "",
                               "query": "lilia"}

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
