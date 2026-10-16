from stats import mean, median, peak, summary


def test_mean():
    assert mean([20.0, 30.0, 40.0]) == 30.0


def test_median_odd():
    assert median([300.0, 100.0, 200.0]) == 200.0


def test_median_even():
    assert median([100.0, 400.0, 200.0, 300.0]) == 250.0


def test_peak():
    assert peak([20.0, 410.0, 380.0]) == (1, 410.0)


def test_summary_count():
    assert summary([20.0, 410.0, 380.0])["count"] == 3
