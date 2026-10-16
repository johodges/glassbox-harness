"""
fire_calculation: fire-engineering hand calculations, done the way an engineer does them by hand.

The expertise lives HERE, in plain Python and in the tool's description, not in the LLM:

  PICK CORRELATION   the name the LLM wrote -> which equation, which inputs it needs, its reference
  CHECK UNITS/RANGE  every input converted to one unit ("1 MW" -> 1000 kW, "13 ft" -> 3.96 m), and REFUSED
                     with the reason if it is outside the range the correlation holds for
  CALCULATE          the equation itself; the result goes back with the inputs it used, the equation and
                     its reference, so the answer can be checked

Llama 3.2 3B only picks the correlation and copies the numbers, with their units, out of the request. It
never does the arithmetic: a 3B model cannot reliably raise 1000 to the power 2/5, and nobody should trust
a language model's arithmetic in a fire-safety calculation anyway.
"""

import math
import re

from .base import Refusal, Tool

# ---- 1. Units: each quantity is converted to ONE unit before any equation sees it -------------------------
SI = {"power": "kW", "length": "m", "time": "s", "temperature": "°C", "rti": "m^1/2 s^1/2", "alpha": "kW/s^2",
      "growth": "kW/s^2 (alpha)"}
UNITS = {  # quantity -> {unit, as normalized by _unit() -> factor to the unit in SI above}
    "power": {"kw": 1, "w": 1e-3, "mw": 1e3, "btu/s": 1.055},
    "length": {"m": 1, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254},
    "time": {"s": 1, "sec": 1, "min": 60, "h": 3600},
    "rti": {"m1/2s1/2": 1, "ms1/2": 1, "√ms": 1, "sqrtms": 1, "ft1/2s1/2": 0.3048 ** 0.5, "fts1/2": 0.3048 ** 0.5},
    "alpha": {"kw/s2": 1},
}
TEMPERATURE = {"c": lambda v: v, "k": lambda v: v - 273.15, "f": lambda v: (v - 32) / 1.8}  # -> °C
NUMBER = re.compile(r"^\s*([-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(.*?)\s*$", re.I)

# The inputs the LLM may give: name -> (quantity, what it is, an example to copy). This also writes the schema.
INPUTS = {
    "hrr": ("power", "heat release rate of the fire", "1 MW"),
    "diameter": ("length", "diameter of the fire (the pool or burning area)", "1 m"),
    "time": ("time", "time since ignition", "120 s"),
    "growth": ("growth", "t-squared growth class: slow, medium, fast or ultrafast", "fast"),
    "ceiling_height": ("length", "height of the ceiling above the fire", "4 m"),
    "radius": ("length", "horizontal distance from the fire's axis to the sprinkler or detector", "3 m"),
    "rti": ("rti", "response time index of the sprinkler or detector", "100 m^1/2 s^1/2"),
    "activation_temperature": ("temperature", "activation temperature of the sprinkler or detector", "68 C"),
    "ambient_temperature": ("temperature", "air temperature before the fire", "20 C"),
}

# t-squared fires reach 1055 kW (1000 Btu/s) after 600, 300, 150 or 75 s, so alpha = 1055 / t_g^2.
GROWTH_TIME = {"slow": 600, "medium": 300, "fast": 150, "ultrafast": 75}
ALPHA = {name: 1055 / t_g ** 2 for name, t_g in GROWTH_TIME.items()}  # 0.00293, 0.0117, 0.0469, 0.188 kW/s^2


def _unit(text):
    """'m^0.5 s^0.5', '(m·s)^½' and 'm1/2s1/2' all become one spelling; '°C' -> 'c'."""
    text = text.lower().replace("½", "1/2").replace("0.5", "1/2").replace("²", "2")
    return re.sub(r"[\s°^·*()]|deg(?=[cfk]$)", "", text)


def to_si(name, raw):
    """'1 MW' for hrr -> (1000.0, '1 MW = 1000 kW'). Raises ValueError (sent back to the LLM) if it can't."""
    quantity, what, example = INPUTS[name]
    if quantity == "growth":
        word = re.sub(r"[^a-z]", "", str(raw).lower())
        for growth in ("ultrafast", "slow", "medium", "fast"):  # "ultra-fast" before "fast"
            if growth in word:
                return ALPHA[growth], f"growth '{raw}' -> {growth}: alpha = {ALPHA[growth]:.4g} kW/s^2 (NFPA 72)"
        number = NUMBER.match(str(raw))
        if number and _unit(number.group(2)) in ("", "kw/s2"):
            return float(number.group(1).replace(",", "")), f"growth {raw} -> alpha = {number.group(1)} kW/s^2"
        raise ValueError(f"growth = '{raw}': write slow, medium, fast or ultrafast (or alpha in kW/s^2)")
    number = NUMBER.match(str(raw))
    if not number:
        raise ValueError(f"{name} = '{raw}' is not a number with a unit. Write it like '{example}'.")
    value, written = float(number.group(1).replace(",", "")), number.group(2)
    # the unit as written ("m^1/2 s^1/2"), else its first word ("3 m radius" -> "m")
    unit = next((u for u in (_unit(written), _unit(written.split()[0]) if written else "")
                 if u in UNITS.get(quantity, {}) or u in TEMPERATURE), _unit(written))
    if not unit:
        return value, f"{name} = {raw}: no unit given, read as {SI[quantity]}"
    if quantity == "temperature" and unit in TEMPERATURE:
        return TEMPERATURE[unit](value), f"{name} = {raw} = {TEMPERATURE[unit](value):.4g} °C"
    if unit in UNITS.get(quantity, {}):
        si = value * UNITS[quantity][unit]
        return si, f"{name} = {raw}" + ("" if UNITS[quantity][unit] == 1 else f" = {si:.4g} {SI[quantity]}")
    other = next((q for q, table in UNITS.items() if unit in table), "temperature" if unit in TEMPERATURE else None)
    if other:
        raise ValueError(f"{name} = '{raw}' is a {other}, but {name} is the {what}: a {quantity}, e.g. '{example}'.")
    raise ValueError(f"{name} = '{raw}': unknown unit '{number.group(2)}'. Write it like '{example}'.")


# ---- 2. The correlations: equation, reference, inputs, range check and the calculation itself -----------
def flame_tip(hrr_kw, diameter_m=0.0):
    """Heskestad (1983) mean flame height in m. diameter 0 = a point source, the tallest flame for this HRR."""
    return 0.235 * hrr_kw ** 0.4 - 1.02 * diameter_m


def alpert(hrr_kw, height_m, radius_m):
    """Alpert (1972) maximum ceiling-jet temperature rise (K) and velocity (m/s) for a steady fire."""
    ratio = radius_m / height_m
    rise = 16.9 * hrr_kw ** (2 / 3) / height_m ** (5 / 3) if ratio <= 0.18 else 5.38 * (hrr_kw / radius_m) ** (2 / 3) / height_m
    speed = 0.96 * (hrr_kw / height_m) ** (1 / 3) if ratio <= 0.15 else 0.195 * hrr_kw ** (1 / 3) * height_m ** 0.5 / radius_m ** (5 / 6)
    return rise, speed


def t_squared_fire(v):
    if "time" in v:
        return {"hrr_kW": v["growth"] * v["time"] ** 2}
    return {"time_to_reach_s": math.sqrt(v["hrr"] / v["growth"])}


def flame_height(v):
    return {"flame_height_m": flame_tip(v["hrr"], v["diameter"])}


def ceiling_jet(v):
    rise, speed = alpert(v["hrr"], v["ceiling_height"], v["radius"])
    return {"gas_temperature_C": v["ambient_temperature"] + rise, "temperature_rise_K": rise, "gas_velocity_m_s": speed}


def sprinkler_activation(v, dt=0.1, give_up_s=3600):
    """
    DETACT-QS: the fire grows as Q = alpha t^2; at every time step Alpert gives the gas temperature and velocity at
    the sprinkler, and the link heats up as dT_link/dt = sqrt(u) / RTI * (T_gas - T_link). Step until it activates.
    """
    t, link = 0.0, v["ambient_temperature"]
    while link < v["activation_temperature"]:
        t += dt
        if t > give_up_s:
            return {"activates": "no, not within 1 hour"}
        hrr = v["growth"] * t ** 2
        rise, speed = alpert(hrr, v["ceiling_height"], v["radius"])
        gas = v["ambient_temperature"] + rise
        link += (gas - link) * (1 - math.exp(-math.sqrt(speed) * dt / v["rti"]))  # exact for one step
    return {"activation_time_s": t, "activation_time_min": t / 60, "hrr_at_activation_kW": hrr,
            "gas_temperature_C": gas, "gas_velocity_m_s": speed}


def check(name, v):
    """-> (refusals, cautions). A refusal stops the calculation; a caution goes back with the result."""
    refuse, caution = [], []
    for key, value in v.items():  # physics first: nothing here can be zero or negative
        if key in ("hrr", "diameter", "ceiling_height", "rti", "growth") and value <= 0:
            refuse.append(f"{key} must be greater than zero (it is {value:g})")
        if key in ("radius", "time") and value < 0:
            refuse.append(f"{key} cannot be negative (it is {value:g})")
    if v.get("activation_temperature", 1e9) <= v.get("ambient_temperature", -1e9):
        refuse.append(f"the activation temperature ({v['activation_temperature']:g} °C) must be above the ambient "
                      f"temperature ({v['ambient_temperature']:g} °C), or it would never need the fire to activate")
    if refuse:
        return refuse, caution
    if name == "flame_height":
        x = v["hrr"] ** 0.4 / v["diameter"]
        if not 7 <= x <= 700:
            refuse.append(f"Q^(2/5)/D = {x:.3g} kW^(2/5)/m is outside 7 to 700, the range Heskestad's correlation "
                          "was fitted on (NUREG-1805). " + (
                              f"The fire is too small for its {v['diameter']:g} m diameter: its flames break up into "
                              f"flamelets, and the equation would give L = {flame_tip(v['hrr'], v['diameter']):.2f} m."
                              if x < 7 else "The source is too small for this heat release rate (closer to a jet flame)."))
    if name in ("ceiling_jet", "sprinkler_activation"):
        hrr = v["hrr"] if name == "ceiling_jet" else sprinkler_activation(v).get("hrr_at_activation_kW")
        if hrr and flame_tip(hrr, v.get("diameter", 0)) >= v["ceiling_height"]:
            caution.append(f"At {hrr:.0f} kW the flames (Heskestad, {flame_tip(hrr, v.get('diameter', 0)):.1f} m) "
                           f"reach the {v['ceiling_height']:g} m ceiling. Alpert's correlations describe the hot gas "
                           "beyond the flames, so the ceiling-jet values here are only approximate.")
    if name == "t_squared_fire" and t_squared_fire(v).get("hrr_kW", 0) > 10_000:
        caution.append("Above 10 MW: check that the fuel and the ventilation could really support a fire this big.")
    return refuse, caution


CORRELATIONS = {
    "t_squared_fire": {
        "title": "t-squared fire growth",
        "equation": "Q = alpha * t^2  (or t = sqrt(Q / alpha))",
        "reference": "NFPA 72, Annex B: t-squared fires; alpha = 1055 kW / t_g^2, t_g = 600, 300, 150, 75 s",
        "needs": ("growth",), "one_of": ("time", "hrr"), "defaults": {},
        "assumes": "the growth phase only: it does not know when the fuel or the oxygen runs out",
        "calculate": t_squared_fire,
    },
    "flame_height": {
        "title": "Heskestad flame height",
        "equation": "L = 0.235 * Q^(2/5) - 1.02 * D   (L, D in m; Q in kW)",
        "reference": "G. Heskestad, Luminous heights of turbulent diffusion flames, Fire Safety Journal 5 (1983) "
                     "103-108; range 7 < Q^(2/5)/D < 700 as in NUREG-1805 (2004)",
        "needs": ("hrr", "diameter"), "defaults": {},
        "assumes": "a round fire in open air at normal temperature and pressure; L is the mean (50 %) flame height",
        "calculate": flame_height,
    },
    "ceiling_jet": {
        "title": "Alpert ceiling jet",
        "equation": "r/H <= 0.18: dT = 16.9 Q^(2/3) / H^(5/3), else dT = 5.38 (Q/r)^(2/3) / H;  "
                    "r/H <= 0.15: u = 0.96 (Q/H)^(1/3), else u = 0.195 Q^(1/3) H^(1/2) / r^(5/6)",
        "reference": "R.L. Alpert, Calculation of response time of ceiling-mounted fire detectors, "
                     "Fire Technology 8 (1972) 181-195",
        "needs": ("hrr", "ceiling_height", "radius"), "defaults": {"ambient_temperature": (20.0, "20 °C")},
        "assumes": "a steady fire under a large, flat, smooth, unconfined ceiling, away from walls",
        "calculate": ceiling_jet,
    },
    "sprinkler_activation": {
        "title": "DETACT-QS sprinkler / heat detector activation",
        "equation": "Q = alpha t^2;  T_gas, u from Alpert;  dT_link/dt = sqrt(u) / RTI * (T_gas - T_link);  "
                    "step 0.1 s until T_link >= T_activation",
        "reference": "D.D. Evans and D.W. Stroup, Methods to calculate the response time of heat and smoke "
                     "detectors installed below large unobstructed ceilings, Fire Technology 22 (1986) 54-65 "
                     "(DETACT-QS); ceiling jet from Alpert (1972)",
        "needs": ("growth", "ceiling_height", "radius", "rti"),
        "defaults": {"activation_temperature": (68.0, "68 °C, a common ordinary-temperature sprinkler"),
                     "ambient_temperature": (20.0, "20 °C")},
        "assumes": "quasi-steady gas flow (no transport lag) and no heat lost to the pipe (no C-factor), so it tends "
                   "to predict activation slightly early; an unconfined flat ceiling",
        "calculate": sprinkler_activation,
    },
}
# What the LLM may write for each correlation name (it rarely writes the exact key)
ALIASES = [("sprinkler_activation", r"sprinkler|detector|detact|activat|rti|response"),
           ("flame_height", r"flame|heskestad"),
           ("ceiling_jet", r"ceiling|alpert|jet"),
           ("t_squared_fire", r"t_?squared|t2|t²|growth|hrr|heat_release")]
# Words in the USER's request that show which quantities they asked for (to notice a forgotten second one)
ASKED = {"flame_height": r"flame height|how (?:tall|high) is the flame|tall .{0,20}flame",
         "sprinkler_activation": r"sprinkler|detector|activat",
         "ceiling_jet": r"ceiling[- ]jet|gas temperature|temperature .{0,30}ceiling"}


def pick(name):
    key = re.sub(r"[\s-]+", "_", str(name).strip().lower())
    if key in CORRELATIONS:
        return key
    for correlation, pattern in ALIASES:
        if re.search(pattern, key):
            return correlation
    raise ValueError(f"There is no correlation called '{name}'. Use one of: {', '.join(CORRELATIONS)}.")


def _round(value):
    return float(f"{value:.3g}") if isinstance(value, float) else value


def summary(name, r, v):
    """One self-contained sentence per result: a 3B model copies a line like this better than it reads a dict
    (Llama made both calculations, then answered with the flame height only)."""
    if name == "flame_height":
        return f"Flame height (Heskestad): {r['flame_height_m']} m for {_round(v['hrr']):g} kW on a {v['diameter']:g} m fire."
    if name == "ceiling_jet":
        return (f"Ceiling jet (Alpert) at r = {v['radius']:g} m under a {v['ceiling_height']:g} m ceiling: "
                f"{r['gas_temperature_C']} °C, {r['gas_velocity_m_s']} m/s.")
    if name == "sprinkler_activation":
        if "activates" in r:
            return "Sprinkler / detector activation (DETACT-QS): it does not activate within 1 hour."
        return (f"Sprinkler / detector activation (DETACT-QS): {r['activation_time_s']:g} s "
                f"({r['activation_time_min']} min), when the fire is {r['hrr_at_activation_kW']:g} kW.")
    if "hrr_kW" in r:
        return f"t-squared fire: {r['hrr_kW']:g} kW at {v['time']:g} s."
    return f"t-squared fire: it reaches {_round(v['hrr']):g} kW after {r['time_to_reach_s']:g} s."


# =========================================================================================
class FireCalculationTool(Tool):
    name = "fire_calculation"
    description = (
        "Fire-engineering hand calculations with standard correlations. One correlation per call: call it again for "
        "each quantity the user asks for. correlation is one of: "
        "t_squared_fire (heat release rate at a time: growth + time; or the time to reach a heat release rate: "
        "growth + hrr), "
        "flame_height (Heskestad: hrr + diameter), "
        "ceiling_jet (Alpert ceiling-jet temperature and velocity: hrr + ceiling_height + radius), "
        "sprinkler_activation (DETACT activation time of a sprinkler or heat detector in a t-squared fire: growth + "
        "ceiling_height + radius + rti, optional activation_temperature). "
        "Give every number with its unit, as the user wrote it. The tool converts the units, refuses inputs outside a "
        "correlation's valid range, and returns the result with the equation and its reference.")
    parameters = {
        "type": "object",
        "properties": {
            "correlation": {"type": "string",
                            "description": "t_squared_fire, flame_height, ceiling_jet or sprinkler_activation"},
            **{key: {"type": "string", "description": f"The {what}, e.g. '{example}'"}
               for key, (quantity, what, example) in INPUTS.items()},
        },
        "required": ["correlation"],
    }
    model_id = "fire_calcs.py: textbook correlations in plain Python (no ML)"
    how_it_works = ("No model here: the equations are ordinary Python, written once by a person who knows them. "
                    "The LLM picks the correlation and copies the numbers; Python converts the units, checks the "
                    "range, and does the arithmetic.")

    def __init__(self):
        self.done = {}  # run folder -> the correlations calculated in that request

    def run(self, ctx, correlation, **given):
        t = ctx.tracer
        with t.stage("PICK CORRELATION", "python", "fire_calcs.pick() (no ML)", "the name the LLM wrote",
                     "equation + reference") as ev:
            t.say(self.how_it_works, kind="python")
            name = pick(correlation)
            spec = CORRELATIONS[name]
            t.say(f"equation:  {spec['equation']}\nreference: {spec['reference']}", kind="python")
            ev.summary = f"'{correlation}' -> {spec['title']}"

        with t.stage("CHECK UNITS/RANGE", "python", "fire_calcs.to_si() + check() (no ML)", "inputs as text",
                     "inputs in SI units, or a refusal") as ev:
            missing = [k for k in spec["needs"] if not given.get(k)]
            if "one_of" in spec and not any(given.get(k) for k in spec["one_of"]):
                missing.append(" or ".join(spec["one_of"]))
            if missing:
                ev.summary = f"missing: {', '.join(missing)}"
                raise ValueError(f"{name} needs {', '.join(missing)}: " + "; ".join(
                    f"{k} = the {INPUTS[k][1]}, e.g. '{INPUTS[k][2]}'" for k in missing if k in INPUTS))
            wanted = set(spec["needs"]) | set(spec.get("one_of", ())) | set(spec["defaults"])
            values, lines = {}, []
            for key, raw in given.items():
                if key in wanted and str(raw).strip():
                    values[key], line = to_si(key, raw)
                    lines.append(line)
            ignored = [key for key, raw in given.items() if key not in wanted and str(raw).strip()]
            assumed = []
            for key, (value, label) in spec["defaults"].items():
                if key not in values:
                    values[key] = value
                    assumed.append(f"{key.replace('_', ' ')} {label} (not given)")
            for line in lines + [f"assumed: {a}" for a in assumed] + [f"ignored (not used by {name}): {k}" for k in ignored]:
                t.say(line, kind="python")
            refusals, cautions = check(name, values)
            for line in refusals + cautions:
                t.say(("REFUSED: " if line in refusals else "caution: ") + line, kind="python")
            ev.summary = (f"REFUSED: {refusals[0]}" if refusals else f"{len(values)} inputs in SI units, in range"
                          + (f", {len(cautions)} caution(s)" if cautions else ""))
            ev.details = {"inputs_si": values, "assumed": assumed, "refusals": refusals, "cautions": cautions}
        if refusals:  # raised outside the stage, so the trace shows the reason rather than "FAILED"; a Refusal
            # also tells the agent loop not to push the LLM into trying again (see agent.py)
            raise Refusal(f"Refused by the harness: {' '.join(refusals)} Do not try again with other numbers or "
                          "another correlation: tell the user why this correlation does not apply.")

        with t.stage("CALCULATE", "python", f"{spec['title']} (plain Python)", "inputs in SI units", "result") as ev:
            result = {key: _round(value) for key, value in spec["calculate"](values).items()}
            ev.summary = ", ".join(f"{key} = {value}" for key, value in result.items())
        done = self.done.setdefault(ctx.run_dir, set())
        done.add(name)
        asked = [n for n, pattern in ASKED.items() if re.search(pattern, ctx.request, re.I) and n not in done]
        if asked:  # e.g. the user asked for a flame height AND a sprinkler time: the agent's completion check asks once
            ctx.pending[self.name] = f"the user also asked about {' and '.join(asked)}, which has not been calculated"
        else:
            ctx.pending.pop(self.name, None)
        return {"artifacts": [], "observations": {
            "summary": summary(name, result, values), "correlation": spec["title"], "result": result,
            "equation": spec["equation"],
            "inputs_used": {key: f"{_round(value)} {SI[INPUTS[key][0]]}" for key, value in values.items()},
            "assumed": assumed + [spec["assumes"]], "cautions": cautions, "reference": spec["reference"]}}
