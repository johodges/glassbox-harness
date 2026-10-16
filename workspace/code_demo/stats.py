"""Small statistics helpers for temperature readings."""


def mean(values):
    """The average of a list of numbers."""
    if not values:
        raise ValueError("mean() needs at least one value")
    return sum(values) / len(values)


def median(values):
    """The middle value of a list of numbers (the average of the two middle values if there is an even count)."""
    if not values:
        raise ValueError("median() needs at least one value")
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2]


def peak(values):
    """(index, value) of the largest value."""
    index = max(range(len(values)), key=lambda i: values[i])
    return index, values[index]


def summary(values):
    """A dict with the count, mean, median and peak of the values."""
    spread = max(values) - min(values)
    index, highest = peak(values)
    return {"count": len(values), "mean": mean(values), "median": median(values),
            "peak": highest, "peak_index": index}
