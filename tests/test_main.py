"""Tests for main.run_check -- the orchestration layer, and the last
uncovered logic in the repo.

What matters here isn't that a check happens, it's *when a notification
fires*. run_check compares each freshly fetched status against the row it
read at the top of the loop, and a wrong comparison either spams the phone
or silently swallows a closure. The per-restaurant try/except matters for
the same reason: it's what stops one dead place_id from cancelling everyone
else's check.

External calls (Places, Claude, ntfy/SMTP) are faked; the database is real
(a temp file), so the state these tests assert on is written and read back
exactly as it is in production.

Two notification bugs were found while writing these tests -- closing-soon
alerts re-firing every news cycle, and temporary-to-permanent closures going
unannounced. Both are fixed; the tests that cover them say in their
docstrings what the old behaviour was, since that's what they exist to stop
coming back.
"""
import logging

import db
import main


def _news(closing_soon, confidence="high", summary=""):
    """A check_closing_soon() return value."""
    return {"closing_soon": closing_soon, "confidence": confidence, "summary": summary}


def _setup(tmp_path, monkeypatch, restaurants=(("Lilia", "place-lilia"),)):
    """Point db and the run counter at tmp_path, then seed restaurants.

    The run counter is a file in the real data/ directory, so leaving it
    unpatched would let a test run mutate the user's news-check cadence.
    Cadence/retention env vars are cleared for the same reason: main reads
    them at call time, so a developer's .env would otherwise steer the tests
    that rely on the defaults.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(main, "_run_count_file", str(tmp_path / ".run_count"))
    for var in ("CLOSING_SOON_CHECK_EVERY", "CHECK_LOG_RETAIN_DAYS"):
        monkeypatch.delenv(var, raising=False)
    db.init_db()
    for name, place_id in restaurants:
        db.add_restaurant(name, place_id, address=f"{name} address")


def _patch_places(monkeypatch, by_place_id):
    """Fake the Places status poll. A value may be a status string, or an
    Exception instance to raise for that one restaurant."""
    calls = []

    def _fake(place_id):
        calls.append(place_id)
        result = by_place_id[place_id]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main, "get_business_status", _fake)
    return calls


def _patch_news(monkeypatch, by_name=None):
    """Fake the Claude news check. Defaults to 'no closure signal'. A value
    may be a result dict or an Exception instance to raise, same convention
    as _patch_places -- closure_checker lets failures through once the SDK's
    retries are spent, so run_check has to handle one."""
    calls = []

    def _fake(name, address):
        calls.append((name, address))
        result = (by_name or {}).get(name, _news(False))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main, "check_closing_soon", _fake)
    return calls


def _patch_notifiers(monkeypatch, closed_raises=None):
    closed, closing_soon = [], []

    def _closed(restaurant, status):
        if closed_raises is not None:
            raise closed_raises
        closed.append((restaurant["name"], status))

    monkeypatch.setattr(main, "notify_closed", _closed)
    monkeypatch.setattr(main, "notify_closing_soon",
                        lambda r, summary: closing_soon.append((r["name"], summary)))
    return closed, closing_soon


def _row(name):
    return next(r for r in db.list_restaurants(active_only=False) if r["name"] == name)


# --- news-check cadence -------------------------------------------------

def test_news_check_runs_only_on_every_nth_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    news = _patch_news(monkeypatch)
    # Set after import on purpose: main reads the cadence at call time, the
    # same way notifier/places_client read theirs.
    monkeypatch.setenv("CLOSING_SOON_CHECK_EVERY", "3")

    main.run_check()  # run 1
    main.run_check()  # run 2
    assert news == []

    main.run_check()  # run 3 -- the Nth
    assert news == [("Lilia", "Lilia address")]


def test_include_news_check_overrides_the_counter(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    news = _patch_news(monkeypatch)
    monkeypatch.setenv("CLOSING_SOON_CHECK_EVERY", "100")

    main.run_check(include_news_check=True)

    assert len(news) == 1


def test_news_check_skipped_for_non_operational_restaurants(tmp_path, monkeypatch):
    """The expensive call is pointless once Places already says it's closed."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_TEMPORARILY"})
    _patch_notifiers(monkeypatch)
    news = _patch_news(monkeypatch)

    main.run_check(include_news_check=True)

    assert news == []


def test_failed_news_check_keeps_the_status_and_the_stored_flag(tmp_path, monkeypatch, caplog):
    """A dead news check must not cost us the Places status we already paid
    for, and must not clear a flag whose alert has already gone out -- that
    would re-fire the alert on the next successful news check."""
    caplog.set_level(logging.INFO)
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    closed, closing_soon = _patch_notifiers(monkeypatch)

    # Run 1: a real closure signal, so the flag is set and alerted on.
    _patch_news(monkeypatch, {"Lilia": _news(True, summary="Closing in March.")})
    main.run_check(include_news_check=True)
    assert len(closing_soon) == 1

    # Run 2: the news check dies after its retries.
    _patch_news(monkeypatch, {"Lilia": RuntimeError("Anthropic API is down")})
    main.run_check(include_news_check=True)

    row = _row("Lilia")
    assert row["closing_soon_flag"] == 1
    assert row["closing_soon_summary"] == "Closing in March."
    assert row["business_status"] == "OPERATIONAL"
    assert len(closing_soon) == 1  # not re-alerted
    assert closed == []
    # The status check still counts as a check: two runs, two logged checks.
    # (Before the news check got its own try/except, run 2 was abandoned
    # before update_check_result and this stayed at 1.)
    assert db.check_history(row["id"])[0]["checks"] == 2


def test_failed_news_check_is_not_counted_as_a_restaurant_failure(tmp_path, monkeypatch, caplog):
    """The status check succeeded, so the restaurant isn't a failure -- but
    the dead news check still has to be visible in the log."""
    caplog.set_level(logging.INFO)
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": RuntimeError("Anthropic API is down")})

    main.run_check(include_news_check=True)

    assert "0/1 restaurants failed" in caplog.text
    assert "Anthropic API is down" in caplog.text  # traceback, not just a count
    assert "1 news check(s) failed" in caplog.text


def test_failed_news_check_does_not_block_other_restaurants(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch,
           restaurants=(("Lilia", "place-lilia"), ("Don Angie", "place-don-angie")))
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL",
                                "place-don-angie": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    news = _patch_news(monkeypatch, {"Lilia": RuntimeError("Anthropic API is down"),
                                     "Don Angie": _news(True, summary="Last service Sunday.")})

    main.run_check(include_news_check=True)

    assert len(news) == 2
    assert _row("Don Angie")["closing_soon_flag"] == 1


# --- notify on transition ----------------------------------------------

def test_permanent_closure_notifies_and_archives(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_PERMANENTLY"})
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)

    assert closed == [("Lilia", "CLOSED_PERMANENTLY")]
    assert _row("Lilia")["archived"] == 1
    assert db.list_restaurants(active_only=True) == []


def test_temporary_closure_notifies_but_stays_active(tmp_path, monkeypatch):
    """Temporary closures keep getting polled -- they may reopen."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_TEMPORARILY"})
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)

    assert closed == [("Lilia", "CLOSED_TEMPORARILY")]
    assert _row("Lilia")["archived"] == 0


def test_closure_does_not_re_notify_on_later_runs(tmp_path, monkeypatch):
    """Second run sees the stored status is already closed -- no transition."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_TEMPORARILY"})
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)
    main.run_check(include_news_check=False)

    assert len(closed) == 1


def test_still_operational_notifies_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    closed, closing_soon = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=True)

    assert (closed, closing_soon) == ([], [])
    assert _row("Lilia")["last_checked_at"] is not None


def test_temporary_to_permanent_closure_notifies_and_archives(tmp_path, monkeypatch):
    """A place can close temporarily and *then* for good. Gating on
    `was_operational` used to make that second hop silent and leave the row
    active, polled forever."""
    _setup(tmp_path, monkeypatch)
    statuses = {"place-lilia": "CLOSED_TEMPORARILY"}
    _patch_places(monkeypatch, statuses)
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)
    statuses["place-lilia"] = "CLOSED_PERMANENTLY"
    main.run_check(include_news_check=False)

    assert closed == [("Lilia", "CLOSED_TEMPORARILY"), ("Lilia", "CLOSED_PERMANENTLY")]
    assert _row("Lilia")["archived"] == 1


def test_stranded_permanent_closure_gets_archived(tmp_path, monkeypatch):
    """Archiving keys off the status, not the transition, so a row left
    active by a failed earlier run (or by the old gating bug) is swept up on
    the next run even though there's no transition left to notify on."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_PERMANENTLY"})
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)
    # Already recorded as permanently closed, but never archived.
    db.update_check_result(_row("Lilia")["id"], "CLOSED_PERMANENTLY", False, "")

    main.run_check(include_news_check=False)

    assert closed == []  # nothing new to announce
    assert _row("Lilia")["archived"] == 1


# --- closing-soon flag --------------------------------------------------

def test_closing_soon_notifies_when_flag_first_set(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _, closing_soon = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": _news(True, "high", "Closing after 10 years")})

    main.run_check(include_news_check=True)

    assert closing_soon == [("Lilia", "Closing after 10 years")]
    row = _row("Lilia")
    assert row["closing_soon_flag"] == 1
    assert row["closing_soon_summary"] == "Closing after 10 years"


def test_closing_soon_ignores_low_confidence(tmp_path, monkeypatch):
    """Only medium/high confidence counts -- low is treated as no signal."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _, closing_soon = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": _news(True, "low", "A blog speculated")})

    main.run_check(include_news_check=True)

    assert closing_soon == []
    assert _row("Lilia")["closing_soon_flag"] == 0


def test_closing_soon_does_not_re_notify_on_back_to_back_news_runs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _, closing_soon = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": _news(True, "high", "Closing after 10 years")})

    main.run_check(include_news_check=True)
    main.run_check(include_news_check=True)

    assert len(closing_soon) == 1


def test_non_news_run_preserves_the_closing_soon_flag(tmp_path, monkeypatch):
    """Only a news check can change the flag. Hardcoding False on plain runs
    used to wipe it (while the summary text survived), which is what let the
    next news run re-alert."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": _news(True, "high", "Closing after 10 years")})

    main.run_check(include_news_check=True)
    assert _row("Lilia")["closing_soon_flag"] == 1

    main.run_check(include_news_check=False)

    assert _row("Lilia")["closing_soon_flag"] == 1
    assert _row("Lilia")["closing_soon_summary"] == "Closing after 10 years"


def test_closing_soon_does_not_re_notify_after_an_intervening_plain_run(tmp_path, monkeypatch):
    """The realistic cadence: news run, some plain runs, news run. At the
    default CLOSING_SOON_CHECK_EVERY=4 the cleared-flag bug re-alerted about
    the same closure every 4th run, indefinitely."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _, closing_soon = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch, {"Lilia": _news(True, "high", "Closing after 10 years")})

    main.run_check(include_news_check=True)
    main.run_check(include_news_check=False)
    main.run_check(include_news_check=False)
    main.run_check(include_news_check=True)

    assert len(closing_soon) == 1


def test_news_check_can_clear_a_previously_set_closing_soon_flag(tmp_path, monkeypatch):
    """Carrying the flag forward must not make it sticky -- a later news run
    finding no signal still clears it, which re-arms the alert if the news
    comes back."""
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _, closing_soon = _patch_notifiers(monkeypatch)
    news = {"Lilia": _news(True, "high", "Closing after 10 years")}
    _patch_news(monkeypatch, news)

    main.run_check(include_news_check=True)
    news["Lilia"] = _news(False)  # story retracted
    main.run_check(include_news_check=True)

    assert _row("Lilia")["closing_soon_flag"] == 0
    assert len(closing_soon) == 1

    news["Lilia"] = _news(True, "high", "Closing after all")
    main.run_check(include_news_check=True)

    assert len(closing_soon) == 2


# --- per-restaurant failure isolation -----------------------------------

def test_one_failing_restaurant_does_not_stop_the_others(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, restaurants=(
        ("Lilia", "place-lilia"),
        ("Broken", "place-broken"),
        ("Don Angie", "place-angie"),
    ))
    _patch_places(monkeypatch, {
        "place-lilia": "OPERATIONAL",
        "place-broken": RuntimeError("Places API exploded"),
        "place-angie": "CLOSED_PERMANENTLY",
    })
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)  # must not raise

    assert closed == [("Don Angie", "CLOSED_PERMANENTLY")]
    assert _row("Lilia")["last_checked_at"] is not None
    assert _row("Broken")["last_checked_at"] is None  # never got written
    assert _row("Don Angie")["archived"] == 1


def test_failure_is_logged_with_a_traceback(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)  # the end-of-run tally is INFO; caplog defaults to WARNING
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": RuntimeError("Places API exploded")})
    _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)

    assert "Places API exploded" in caplog.text
    assert "1/1 restaurants failed" in caplog.text


def test_notifier_failure_does_not_abort_the_run(tmp_path, monkeypatch):
    """The status write lands before the notify call, so a dead ntfy leaves
    the DB correct -- and the next run won't retry the alert, since the
    transition is already recorded."""
    _setup(tmp_path, monkeypatch, restaurants=(
        ("Lilia", "place-lilia"),
        ("Don Angie", "place-angie"),
    ))
    _patch_places(monkeypatch, {
        "place-lilia": "CLOSED_TEMPORARILY",
        "place-angie": "OPERATIONAL",
    })
    _patch_notifiers(monkeypatch, closed_raises=RuntimeError("ntfy down"))
    _patch_news(monkeypatch)

    main.run_check(include_news_check=False)  # must not raise

    assert _row("Lilia")["business_status"] == "CLOSED_TEMPORARILY"
    assert _row("Don Angie")["last_checked_at"] is not None


# --- housekeeping -------------------------------------------------------

def _patch_prune(monkeypatch, retain_days, raises=None):
    calls = []

    def _fake(days):
        calls.append(days)
        if raises is not None:
            raise raises
        return 0

    monkeypatch.setattr(main, "prune_check_log", _fake)
    monkeypatch.setenv("CHECK_LOG_RETAIN_DAYS", str(retain_days))
    return calls


def test_pruning_runs_after_the_checks(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)
    pruned = _patch_prune(monkeypatch, retain_days=30)

    main.run_check(include_news_check=False)

    assert pruned == [30]


def test_pruning_disabled_by_zero_retention(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "OPERATIONAL"})
    _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)
    pruned = _patch_prune(monkeypatch, retain_days=0)

    main.run_check(include_news_check=False)

    assert pruned == []


def test_pruning_failure_does_not_fail_a_run_whose_checks_landed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _patch_places(monkeypatch, {"place-lilia": "CLOSED_PERMANENTLY"})
    closed, _ = _patch_notifiers(monkeypatch)
    _patch_news(monkeypatch)
    _patch_prune(monkeypatch, retain_days=30, raises=RuntimeError("database is locked"))

    main.run_check(include_news_check=False)  # must not raise

    assert closed == [("Lilia", "CLOSED_PERMANENTLY")]
    assert _row("Lilia")["archived"] == 1
