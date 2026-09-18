import math

from engine import trend_rules as tr


def test_momentum_restart_from_zero_clears_spike_gate():
    # The 2026-09-18 bug: prior=0 & recent>0 returned exactly 1.5, which
    # never satisfies the `> 1.5` spike gate.
    assert tr.momentum(3.64, 0.0) == math.inf
    assert tr.momentum(3.64, 0.0) > tr.SPIKE_MOMENTUM
    assert tr.momentum(0.0, 0.0) == 1.0
    assert tr.momentum(2.0, 7.0) == 2.0 / 7.0
    assert tr.momentum(5.0, 0.5) == 5.0  # prior floored at 1


def test_active_months_and_sporadic():
    bcf = [33.0, 6.7, 0.0, 4.2, 18.8, 1.6, 4.3, 6.0, 0.0, 0.0, 0.0, 3.6]
    assert tr.active_months(bcf) == 3
    assert tr.is_sporadic_months(bcf) is True
    steady = [5, 6, 4, 5, 7, 6, 5, 6, 4, 5, 7, 6]
    assert tr.is_sporadic_months(steady) is False
    # 4 of 6 is the Stable floor
    assert tr.is_sporadic_months([0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 0]) is False
    assert tr.is_sporadic_months([0] * 12) is False
    assert tr.is_sporadic_months(None) is False


def test_restart_label_uses_12mo_diversity():
    # LED-BCF-RGB-IP20-5: 19 buyers over 12mo, top 46.8% → lumpy, not a project
    assert tr.restart_label(19, 0.468) == tr.SPORADIC
    # One buyer all year → project
    assert tr.restart_label(1, 1.0) == tr.PROJECT
    assert tr.restart_label(2, 0.6) == tr.PROJECT
    assert tr.restart_label(5, 0.55) == tr.PROJECT


def test_a_class_grace_requires_evidence():
    # BCF: A, 80/yr, 3.64 in 90d, 1 buyer, top 46.8%, 3 active months → holds
    assert tr.a_class_grace_holds("A", 80.06, 3.64, 1, 0.468, 3) is True
    # Same but only 2 active months and 1 buyer → withdrawn
    assert tr.a_class_grace_holds("A", 80.06, 3.64, 1, 0.468, 2) is False
    # 2 buyers in 45d is enough on its own
    assert tr.a_class_grace_holds("A", 80.06, 3.64, 2, 0.468, 1) is True
    # 90d rate ≥ 20% of 12mo rate keeps grace
    assert tr.a_class_grace_holds("A", 100.0, 5.0, 1, 0.1, None) is True
    assert tr.a_class_grace_holds("A", 100.0, 4.9, 1, 0.1, None) is False
    # Concentrated year never gets grace; non-A never gets grace
    assert tr.a_class_grace_holds("A", 100.0, 50.0, 5, 0.5, 6) is False
    assert tr.a_class_grace_holds("B", 100.0, 50.0, 5, 0.1, 6) is False
    assert tr.a_class_grace_holds("A", 100.0, 0.0, 5, 0.1, 6) is False


def test_sporadic_rate_takes_the_lower_window():
    # 80/yr vs 13.9 in ~170 days → 6mo rate wins
    r = tr.sporadic_daily_rate(80.06, 13.9, 170)
    assert abs(r - 13.9 / 170) < 1e-9
    assert r < 80.06 / 365
    # recent stronger than annual → annual wins (never boosts)
    assert tr.sporadic_daily_rate(52.0, 40.0, 170) == 52.0 / 365
    assert tr.sporadic_daily_rate(0, 0, 170) == 0.0


def test_evidence_text():
    s = tr.evidence_text(1, 3, 0.468, 19)
    assert s == "1 buyer/45d · sold 3/6 mo · top buyer 47% of 12mo"
    s2 = tr.evidence_text(8, 6, 0.18, 29)
    assert s2 == "8 buyers/45d · sold 6/6 mo · 29 buyers/12mo"
