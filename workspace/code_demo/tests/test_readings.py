from readings import parse_line, rolling_mean, time_to_reach


def test_parse_line():
    assert parse_line("12.5, 341.0") == (12.5, 341.0)


def test_rolling_mean_first_value():
    assert rolling_mean([20.0, 40.0, 60.0, 80.0], 2)[0] == 30.0


def test_time_to_reach():
    assert time_to_reach([0, 10, 20, 30], [20.0, 150.0, 400.0, 600.0], 300.0) == 20


def test_time_to_reach_never():
    assert time_to_reach([0, 10], [20.0, 25.0], 300.0) is None
