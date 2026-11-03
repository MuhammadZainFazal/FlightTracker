from datetime import datetime
from decimal import Decimal

from logic import decide_alert, month_key


def test_no_alert_above_threshold():
    assert decide_alert(Decimal("1500"), Decimal("1400"), None).alert is False


def test_first_time_below_threshold_alerts():
    decision = decide_alert(Decimal("1300"), Decimal("1400"), None)
    assert decision.alert is True


def test_exactly_at_threshold_alerts():
    assert decide_alert(Decimal("1400"), Decimal("1400"), None).alert is True


def test_same_price_does_not_alert_again():
    assert decide_alert(Decimal("1300"), Decimal("1400"), Decimal("1300")).alert is False


def test_higher_price_below_threshold_does_not_alert_again():
    assert decide_alert(Decimal("1350"), Decimal("1400"), Decimal("1300")).alert is False


def test_further_drop_alerts_again():
    assert decide_alert(Decimal("1250"), Decimal("1400"), Decimal("1300")).alert is True


def test_price_back_above_threshold_resets():
    decision = decide_alert(Decimal("1600"), Decimal("1400"), Decimal("1300"))
    assert decision.alert is False and decision.reset is True


def test_no_reset_when_never_alerted():
    assert decide_alert(Decimal("1600"), Decimal("1400"), None).reset is False


def test_month_key():
    assert month_key(datetime(2026, 10, 28)) == "2026-10"
