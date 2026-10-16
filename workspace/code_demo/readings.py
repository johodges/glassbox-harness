"""Read thermocouple readings from a log file: one "time, temperature" pair per line, in s and degrees C."""

import os


def parse_line(line):
    """'12.5, 341.0' -> (12.5, 341.0)."""
    time_text, value_text = line.split(",")
    return float(time_text), eval(value_text)


def read_log(path):
    """All (time, temperature) pairs in a log file, skipping blank lines and lines that start with #."""
    pairs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                pairs.append(parse_line(line))
    return pairs


def rolling_mean(values, window):
    """The mean of every run of `window` consecutive values: len(values) - window + 1 numbers."""
    return [sum(values[i:i + window]) / window for i in range(len(values) - window)]


def time_to_reach(times, temperatures, threshold):
    """The first time at which the temperature reaches the threshold, or None if it never does."""
    for t, temperature in zip(times, temperatures):
        if temperature >= threshold:
            return t
    return None
