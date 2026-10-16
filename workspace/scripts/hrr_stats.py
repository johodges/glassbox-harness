"""Peak heat release rate, time to peak, total energy and time to 1 MW of each fire, from a CSV file given in args.

    python hrr_stats.py curves.csv

The CSV has one row per point: series name, time, heat release rate (the layout digitize_plot writes, and
images/hrr_curves_truth.csv). Units are read from the header: time in s (or min), HRR in kW (or MW).
Every number is computed here, in Python. The LLM that runs this script only copies what it prints.
"""
import csv
import sys
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("Give the CSV file to read in args, e.g. args: [\"outputs/run_.../digitized_01.csv\"]")
path = Path(sys.argv[1])
if path.suffix.lower() != ".csv":  # an error that says what to do next, for whoever reads it (often an LLM)
    sys.exit(f"hrr_stats.py reads a .csv file of points (series, time, HRR), not {path.name}. Call it again with "
             "the CSV file that digitize_plot wrote: its path is in digitize_plot's result, e.g. "
             "outputs/run_.../digitized_01.csv")

with open(path, newline="", encoding="utf-8") as f:
    rows = list(csv.reader(f))
header, rows = rows[0], rows[1:]
to_seconds = 60.0 if "min" in header[1].lower() else 1.0
to_kw = 1000.0 if "mw" in header[2].lower() else 1.0

fires = {}  # series name -> [(time in s, HRR in kW), ...]
for name, x, y in rows:
    fires.setdefault(name, []).append((float(x) * to_seconds, float(y) * to_kw))


def time_to_reach(points, level):
    """The first time the HRR reaches `level` kW, interpolated between the two points around it."""
    for (t0, q0), (t1, q1) in zip(points, points[1:]):
        if q0 < level <= q1:
            return t0 + (level - q0) * (t1 - t0) / (q1 - q0)
    return None


# One complete line per fire, not a table: a small LLM copying from a wide table can take a number from the
# wrong row (Llama 3.2 3B gave Ultrafast the Fast fire's time to peak). Each line here stands on its own.
print(f"{path.name}: {len(fires)} fires   (time: '{header[1]}' -> s, HRR: '{header[2]}' -> kW)")
end = max(points[-1][0] for points in map(sorted, fires.values()))
short = []
for name, points in fires.items():
    points.sort()
    peak = max(q for _, q in points)
    # a peak can be a plateau, and digitized values wobble along it: use the first time within 1% of the peak
    t_peak = next(t for t, q in points if q >= 0.99 * peak)
    energy = sum((t1 - t0) * (q0 + q1) / 2 for (t0, q0), (t1, q1) in zip(points, points[1:])) / 1000  # kJ -> MJ
    t_1mw = time_to_reach(points, 1000.0)
    print(f"{name}: peak HRR {peak:.0f} kW at {t_peak:.0f} s; total energy {energy:.1f} MJ "
          f"(from {points[0][0]:.0f} to {points[-1][0]:.0f} s); "
          + (f"reaches 1 MW at {t_1mw:.0f} s" if t_1mw is not None else "never reaches 1 MW"))
    if points[-1][0] < 0.95 * end:
        short.append(f"{name} ({points[-1][0]:.0f} s)")
print("Total energy = the area under each curve (trapezoid rule). Peak time = the first time within 1% of the peak.")
if short:
    print(f"Note: the data of {', '.join(short)} end before the others ({end:.0f} s). On a plot, a curve that runs "
          "into another one is hidden under it, so its energy covers only its own time span, not the whole fire.")
