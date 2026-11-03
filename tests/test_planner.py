from datetime import date, timedelta

from logic import GRID_ANCHOR, plan_dates, planned_watch_id

TODAY = date(2026, 10, 28)


def plan(today=TODAY, min_lead=14, lookahead=91, spacing=13, count=6):
    return plan_dates(today, min_lead, lookahead, spacing, count)


def test_default_window_gives_six_dates():
    assert plan() == [date(2026, 11, 11), date(2026, 11, 24), date(2026, 12, 7),
                      date(2026, 12, 20), date(2027, 1, 2), date(2027, 1, 15)]


def test_dates_always_fall_on_different_weekdays():
    for offset in range(0, 200):
        dates = plan(TODAY + timedelta(days=offset))
        assert len(dates) == 6
        assert len({d.weekday() for d in dates}) == 6


def test_all_dates_inside_the_window():
    for offset in range(0, 120):
        today = TODAY + timedelta(days=offset)
        for d in plan(today):
            assert today + timedelta(days=14) <= d <= today + timedelta(days=91)


def test_count_is_a_cap():
    assert len(plan(count=3)) == 3
    assert len(plan(spacing=7, count=6)) == 6


def test_dates_stay_on_the_fixed_grid():
    for offset in range(0, 60):
        for d in plan(TODAY + timedelta(days=offset)):
            assert (d - GRID_ANCHOR).days % 13 == 0


def test_window_rolls_forward_without_changing_other_dates():
    before, after = plan(TODAY), plan(TODAY + timedelta(days=13))
    assert before[0] not in after            # the nearest date left (too close now)
    assert after[:-1] == before[1:]          # everything else kept its identity
    assert after[-1] > before[-1]            # a new date entered at the far end


def test_the_set_only_changes_once_every_13_days():
    changes = sum(plan(TODAY + timedelta(days=i)) != plan(TODAY + timedelta(days=i + 1)) for i in range(26))
    assert changes == 2


def test_watch_id_contains_config():
    wid = planned_watch_id("AMS", "LHE", date(2026, 12, 5), date(2026, 12, 26), 2, "EUR", 1)
    assert wid == "ams-lhe-20261205-20261226-2p-eur-s1"