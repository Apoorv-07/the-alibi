import pathlib

# ── present.py: the policy, in words — phrasing stays in the one module allowed to do it ───────────────
p = pathlib.Path('/home/user/alibi-twin/alibi/present.py')
t = p.read_text()
anchor = '''        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
        return f"{day:%a %d %b %Y}"
'''
assert anchor in t, 'date_words anchor'
method = '''
    def policy_words(self) -> dict:
        """The institution policy as rows a student can read, in the order the page shows them.

        Two rules, both deliberate. Nothing is invented: a key the policy file does not set is reported as
        *not set*, in words, and an identifier is never dressed up as a value. And the wording describes the
        ledger's actual behaviour — `earliest_safe` is not a preference to paraphrase, it is the rule in
        `Ledger.safe_value`, so the sentence says what that does to a plan.
        """
        pol = self.twin.policy or {}

        def pct(v):
            try:
                return f"{round(float(v) * 100)}%"
            except (TypeError, ValueError):
                return None

        rows: list[tuple[str, str]] = []
        att = pct(pol.get("attendance_threshold"))
        rows.append(("Attendance needed to sit an exam",
                     f"{att} per subject" if att else "not set in this policy"))
        con = pct(pol.get("condonation_floor"))
        rows.append(("Reduced on a certificate",
                     (f"down to {con}, and only if the certificate is filed before "
                      + self.date_words(pol.get("defaulter_freeze")))
                     if con else "no condonation floor is set, so the number above is the only one that applies"))
        lp = pol.get("late_policy_default") or {}
        rows.append(("Late submissions",
                     f"{lp['max_days']} days grace, {lp.get('pct_per_day', 0)}% per day" if lp.get("max_days")
                     else "none: a deadline that passes is a deadline that passed"))
        rows.append(("Term starts", self.date_words(pol.get("term_anchor"))))
        rows.append(("If two sources give different dates",
                     "ALIBI plans against the earliest one — planning against the later date is how a deadline "
                     "is missed" if pol.get("tie_break") in (None, "", "earliest_safe") else
                     "this policy asks for the newest assertion instead, so a later-dated claim replaces the "
                     "earlier one"))
        sf = pct(pol.get("supersede_trust_floor"))
        rows.append(("A newer source may replace an older one",
                     f"only if it is at least {sf} believable" if sf else "not set: a newer source does not "
                     "automatically win"))
        prec = [str(k) for k in (pol.get("precedence") or [])]
        rows.append(("Whose wording is read first in a conflict",
                     ", ".join(prec) if prec else "no precedence list is set, so a conflict stays a conflict"))
        tz = pol.get("tz")
        rows.append(("Time zone", f"{tz} — a date with no time is read as this zone's start of day" if tz
                     else "not set: dates are read as the machine's own"))
        rows.append(("Why these numbers", pol.get("rationale") or "the policy file states no rationale"))

        authority = [{"kind": str(k), "trust": pct(v) or "not a number"}
                     for k, v in sorted((pol.get("authority") or {}).items())]
        return {"name": str(pol.get("name") or "no policy file"), "rows": rows,
                "authority": authority, "empty": not authority}
'''
p.write_text(t.replace(anchor, anchor + method, 1))
print('present.py: Presentation.policy_words added')
