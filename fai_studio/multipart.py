"""Multi-part inspection (Inspection Report / Standard Report).

A report can cover several parts (e.g. a lot of 5-7). Each characteristic stores one reading set per part in
`characteristics.part_readings` (JSON):  {"1": {"lo": ".2502", "hi": ".2508", "d": "Accept"}, "2": {...}}
  lo  - the reading (single-value mode) or the LOW reading (low & high mode)
  hi  - the HIGH reading (low & high mode only)
  d   - that part's disposition for this characteristic (calculated; 'Accept' / 'Reject' / '')
The characteristic's own `results` / `disposition` hold the roll-up, so completeness checks, History and every
existing export keep working: results = 'lowest – highest (n of N parts)', disposition = Reject if any part is
rejected, Accept when every part has a reading and all are accepted.

Report fields: part_count (number of parts), reading_mode ('single' | 'minmax'), part_serials (one per line).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional

from .report_types import INSPECTION, STANDARD
from .tolerance import D, evaluate, has_bonus

MULTI_TYPES = (INSPECTION, STANDARD)
MAX_PARTS = 50


def applies(rep: dict) -> bool:
    """Per-part columns are used on Inspection / Standard reports with >1 part or low & high readings."""
    return (rep or {}).get("report_type") in MULTI_TYPES and (part_count(rep) > 1 or minmax(rep))


def part_count(rep: dict) -> int:
    try:
        return max(1, min(MAX_PARTS, int((rep or {}).get("part_count") or 1)))
    except (TypeError, ValueError):
        return 1


def minmax(rep: dict) -> bool:
    return ((rep or {}).get("reading_mode") or "single") == "minmax"


def serials(rep: dict) -> list[str]:
    """Serial number per part (blank where not entered). Part 1 falls back to the report's serial number."""
    n = part_count(rep)
    lines = [l.strip() for l in ((rep or {}).get("part_serials") or "").splitlines()]
    lines = (lines + [""] * n)[:n]
    if not lines[0] and (rep or {}).get("serial_number") and n >= 1:
        lines[0] = rep["serial_number"].strip()
    return lines


def part_label(rep: dict, i: int) -> str:
    sn = serials(rep)[i - 1] if 0 < i <= part_count(rep) else ""
    return f"Part {i}" + (f"\n{sn}" if sn else "")


def parse(text: Optional[str]) -> dict[int, dict]:
    if not text:
        return {}
    try:
        d = json.loads(text)
    except Exception:
        return {}
    out = {}
    for k, v in (d or {}).items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            out[i] = {"lo": str(v.get("lo") or ""), "hi": str(v.get("hi") or ""), "d": str(v.get("d") or "")}
    return out


def dump(readings: dict[int, dict]) -> str:
    clean = {str(i): {k: v for k, v in r.items() if v} for i, r in sorted(readings.items())
             if (r.get("lo") or r.get("hi"))}
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":")) if clean else ""


def part_disposition(r: dict, lower: str, upper: str, bonus: bool, use_hi: bool = True) -> str:
    vals = " ".join(x for x in (r.get("lo"), r.get("hi") if use_hi else "") if x and x.strip())
    return evaluate(vals, lower, upper, bonus) if vals else ""


def recompute(readings: dict[int, dict], char: dict, use_hi: bool = True) -> dict[int, dict]:
    """Judge every part. use_hi=False (one-value mode): a HIGH kept from low & high mode is ignored, not lost."""
    bonus = has_bonus(char.get("requirement") or "")
    for r in readings.values():
        r["d"] = part_disposition(r, char.get("lower_limit") or "", char.get("upper_limit") or "", bonus, use_hi)
    return readings


def rollup(readings: dict[int, dict], n_parts: int, use_hi: bool = True) -> tuple[str, str]:
    """(results text, disposition) for the characteristic across all parts."""
    if not use_hi:
        readings = {i: dict(r, hi="") for i, r in readings.items()}
    used = {i: r for i, r in readings.items() if 1 <= i <= n_parts and (r.get("lo") or r.get("hi"))}
    if not used:
        return "", ""
    nums: list[Decimal] = []
    words: list[str] = []
    no_lead = True                  # keep the inspector's style: .2502 stays .2502 (no 0.2502)
    for r in used.values():
        for x in (r.get("lo"), r.get("hi")):
            if not x:
                continue
            v = D(x.replace("Ø", ""))
            if v is not None:
                nums.append(v)
                if abs(v) < 1 and not re.match(r"^\s*[Ø]?\s*-?\.", x):
                    no_lead = False
            else:
                words.append(x.strip().upper())
    if nums:
        lo, hi = min(nums), max(nums)
        dec = max((len(str(x).split(".")[1]) if "." in str(x) else 0) for x in nums)
        def f(v):
            t = f"{v:.{dec}f}"
            return re.sub(r"^(-?)0\.", r"\1.", t) if no_lead else t
        text = f"{f(lo)}" if lo == hi else f"{f(lo)} – {f(hi)}"
    else:
        text = ", ".join(sorted(set(words)))
    text += f"  ({len(used)} of {n_parts} parts)" if n_parts > 1 else ""
    ds = [r.get("d") for r in used.values()]
    if "Reject" in ds:
        disp = "Reject"
    elif len(used) == n_parts and all(d == "Accept" for d in ds):
        disp = "Accept"
    else:
        disp = ""
    return text, disp


def value_rejected(value: str, char: dict, part_d: str = "") -> bool:
    """Is this one reading (a low or a high) the out-of-tolerance one? Used to mark just that cell."""
    if part_d != "Reject" or not (value or "").strip():
        return False
    lo, hi = char.get("lower_limit") or "", char.get("upper_limit") or ""
    if not (lo or hi) or D(value.replace("Ø", "")) is None:
        return True
    return evaluate(value, lo, hi, has_bonus(char.get("requirement") or "")) == "Reject"


def rejected_parts(readings: dict[int, dict], n_parts: int) -> list[int]:
    return sorted(i for i, r in readings.items() if 1 <= i <= n_parts and r.get("d") == "Reject")


def part_status(chars: list[dict], n_parts: int) -> list[dict]:
    """Per part: readings entered / characteristics, rejected characteristic numbers, overall status."""
    out = []
    measurable = [c for c in chars if c.get("disposition") != "N/A"]
    for i in range(1, n_parts + 1):
        entered, rej = 0, []
        for c in measurable:
            r = parse(c.get("part_readings")).get(i)
            if r and (r.get("lo") or r.get("hi")):
                entered += 1
                if r.get("d") == "Reject":
                    rej.append(c.get("char_no"))
        status = "Reject" if rej else ("Accept" if measurable and entered == len(measurable) else "Open")
        out.append({"part": i, "entered": entered, "total": len(measurable), "rejected": rej, "status": status})
    return out


def key_of(key: str) -> Optional[tuple[int, str]]:
    """'_p3_hi' -> (3, 'hi')."""
    m = re.fullmatch(r"_p(\d+)_(lo|hi)", key or "")
    return (int(m.group(1)), m.group(2)) if m else None


IMPORT_FIELDS = {
    "part": ["PART", "PART #", "PIECE", "PC", "SAMPLE", "SAMPLE NO", "SERIAL", "SERIAL NO", "S/N", "SN", "UNIT"],
    "results_hi": ["HIGH", "MAX", "MAXIMUM", "HI", "UPPER READING"],
}


def split_pair(text: str) -> tuple[str, str]:
    """'.2480 / .2510', '.2480, .2510', '.2480 .2510' -> ('.2480', '.2510'); one value -> (value, '')."""
    parts = [p for p in re.split(r"\s*[,;/]\s*|\s+(?=[-+.\dØ])", (text or "").strip()) if p]
    nums = [p for p in parts if D(p.replace("Ø", "")) is not None]
    if len(nums) >= 2:
        return nums[0], nums[-1]
    return (text or "").strip(), ""


def assign_imports(recs: list[dict], rep: dict) -> dict[int, dict]:
    """Imported result rows of ONE characteristic -> readings per part.
    A 'part' column may hold the part's serial number or its number (1..N); without one the rows go to part 1, 2,
    3... in file order. In low & high mode a row may carry both readings ('.248 / .251' or a separate High column);
    two rows for the same part become its low and its high."""
    n, mm = part_count(rep), minmax(rep)
    sns = [s.upper() for s in serials(rep)]
    out: dict[int, dict] = {}
    nxt = 1
    for rec in recs:
        val = str(rec.get("results", "")).strip()
        if not val:
            continue
        key = str(rec.get("part", "") or "").strip()
        part = None
        if key:
            if key.upper() in sns and key:
                part = sns.index(key.upper()) + 1
            elif re.fullmatch(r"(?:P|PART\s*)?(\d{1,3})", key, re.I):
                k = int(re.search(r"\d+", key).group(0))
                part = k if 1 <= k <= n else None
        if part is None:
            while nxt in out and (not mm or out[nxt].get("hi")):
                nxt += 1
            part = nxt
        if part > n:
            continue
        r = out.setdefault(part, {"lo": "", "hi": ""})
        hi = str(rec.get("results_hi", "") or "").strip()
        if mm:
            lo, hi2 = (val, hi) if hi else split_pair(val)
            if r["lo"] and not r["hi"] and not hi2:
                r["hi"] = lo                       # second row for this part = its high reading
            else:
                r["lo"], r["hi"] = lo, hi2
        else:
            r["lo"] = val
    return out
