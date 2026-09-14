"""Scratch: why do the numeric-separator patterns not fire? literal strings, no f-strings."""
import re

MONTHS_ALT = ("january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
              "september|sept|sep|october|oct|november|nov|december|dec")

P = {
    "d/m/y": r"\b(\d{1,2})\s*[/\.]\s*(\d{1,2})\s*[/\.]\s*(\d{2,4})\b",
    "y-m-d": r"\b(\d{2,4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\b",
    "hyphen-month": r"\b(\d{1,2})\s*-\s*(?:january|jan|february|feb|march|mar|april|apr|may|"
                    r"june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|"
                    r"december|dec)\s*-\s*(\d{4}|\d{2})\b",
    "dot-month": r"\b(\d{1,2})\s*\.\s*(" + MONTHS_ALT + r")\s*\.\s*(\d{4}|\d{2})\b",
}
T = {
    "d/m/y": ["18/09/2026, 21:52 - Karthi M: hackathon", "26.09.2026 at 9", "09/30/2026"],
    "y-m-d": ["due 2026-10-17 at 10:00", "due 15-Oct-2026 at 10:00"],
    "hyphen-month": ["due 15-Oct-2026 at 10:00"],
    "dot-month": ["Due. Oct. 11. 2026"],
}
for tag, pat in P.items():
    print(f"[{tag}] {pat!r}")
    for s in T[tag]:
        m = re.search(pat, s, re.I)
        print(f"    {str(m.groups() if m else None):32} <- {s!r}")
