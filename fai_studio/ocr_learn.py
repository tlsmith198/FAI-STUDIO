"""OCR learning memory: the reader learns from the corrections people make to OCR-read callouts.

When a callout read by OCR is corrected (in the Auto-Balloon review list or later in the Form 3 table), the pair
"read as" -> "corrected to" is saved in the shared database (table ocr_learned), for every workstation:

  whole  the whole callout      '0.25O +.OO5'  -> '.250 ±.005'
  token  a single word of it    '0.25O'        -> '.250',   '+.OO5' -> '±.005'

A learned fix starts as *learning*. Once the same correction has been made on N different characteristics
(Settings: default 2) it becomes *active* and is applied automatically to new OCR reads. Fixes applied that people
then undo are counted; a fix undone more often than kept switches itself off. Everything is visible and editable in
Settings -> OCR Learning, and every change is written to the Reader Rules change log.

Changes to digits (.250 -> .260, .25 -> .250) are NOT learned: those depend on one picture and would
teach the reader to change correct values on other drawings.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

KINDS = {"whole": "Whole callout", "token": "Word in a callout"}
STATUSES = {"learning": "Learning", "active": "Active", "off": "Off"}
MIN_SIMILAR = 0.5            # below this the user typed a different callout, not a fix of a misread
MAX_TOKEN_FIXES = 3

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


_LOOKALIKE = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                            "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6", "q": "9", "g": "9",
                            "+": "±", "t": "±", "°": "0", "Ø": "0", "ø": "0", "⌀": "0", ",": "."})


def similar(a: str, b: str) -> float:
    """How alike two reads are, counting characters OCR commonly confuses (O/0, l/1, S/5, +/±, Ø/0 …) as alike."""
    return SequenceMatcher(None, a.translate(_LOOKALIKE), b.translate(_LOOKALIKE)).ratio()


def digit_change(a: str, b: str) -> bool:
    """True when a digit was changed, added or removed (.250 -> .260, .25 -> .250). Such changes depend on one
    picture, so they are never learned - only misread letters / symbols are (O -> 0, + -> ±, l -> 1)."""
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if any(ch.isdigit() for ch in a[i1:i2]) or \
                (tag == "insert" and any(ch.isdigit() for ch in b[j1:j2])):
            return True
    return False


def derive(read: str, fixed: str) -> list[tuple[str, str, str]]:
    """(kind, wrong, right) pairs worth learning from one correction."""
    r, f = norm(read), norm(fixed)
    if not r or not f or r == f or similar(r, f) < MIN_SIMILAR or digit_change(r, f):
        return []
    rl, fl = len(r.replace(" ", "")), len(f.replace(" ", ""))
    if abs(rl - fl) > max(2, round(0.2 * max(rl, fl))):
        return []                      # words added or removed ('1.500' -> '1.500 REF'): an edit, not a misread
    out = [("whole", r, f)]
    rt, ft = r.split(" "), f.split(" ")
    if len(rt) == len(ft):
        diffs = [(a, b) for a, b in zip(rt, ft) if a != b]
        if 1 <= len(diffs) <= MAX_TOKEN_FIXES:
            for a, b in diffs:
                if len(a) < 2 or similar(a, b) < 0.4 or digit_change(a, b):
                    continue
                out.append(("token", a, b))
    # the whole-callout pair is only kept when it adds something the word fixes don't already cover
    if len(out) > 1:
        rebuilt = " ".join(dict((a, b) for _, a, b in out[1:]).get(t, t) for t in rt)
        if rebuilt == f:
            out = out[1:]
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


class Memory:
    """The active learned fixes, ready to apply."""

    def __init__(self, rows: Iterable[dict] = ()):
        best: dict[tuple[str, str], list[dict]] = {}
        for r in rows or ():
            if (r.get("status") or "") != "active":
                continue
            best.setdefault((r["kind"], r["wrong"]), []).append(r)
        self.fixes: dict[tuple[str, str], dict] = {}
        for k, lst in best.items():
            lst.sort(key=lambda r: -(int(r.get("hits") or 0) + int(r.get("confirmed") or 0)))
            top = lst[0]
            score = lambda r: int(r.get("hits") or 0) + int(r.get("confirmed") or 0)  # noqa: E731
            if len(lst) > 1 and score(lst[1]) * 2 > score(top):
                continue                   # people corrected this read two different ways: don't guess
            self.fixes[k] = top

    def __len__(self):
        return len(self.fixes)

    def apply(self, text: str) -> tuple[str, list[int]]:
        """-> (fixed text, ids of the learned fixes used). Text that no fix matches comes back unchanged."""
        t = norm(text)
        if not t or not self.fixes:
            return text, []
        w = self.fixes.get(("whole", t))
        if w:
            return w["right"], [int(w["id"])]
        ids, toks = [], []
        for tok in t.split(" "):
            f = self.fixes.get(("token", tok))
            if f:
                toks.append(f["right"])
                ids.append(int(f["id"]))
            else:
                toks.append(tok)
        if not ids:
            return text, []
        return " ".join(toks), ids


def fix_kept(fix: dict, final: str) -> bool:
    """Is a learned fix that was applied still in the text the inspector ended up with?"""
    f = norm(final)
    if fix.get("kind") == "whole":
        return f == fix.get("right")
    right = fix.get("right") or ""

    def same_but_digits(t):          # '.260' still has the fix's '.2?0' letters-to-digits correction
        return len(t) == len(right) and all(a == b or (a.isdigit() and b.isdigit()) for a, b in zip(t, right))
    return any(t == right or same_but_digits(t) for t in f.split(" "))


def fix_note(read: str) -> str:
    return f"learned OCR fix (read '{norm(read)}')"
