"""
Numbers and write-ups from the audit log: how long incidents stayed open, how
fast the engine acted, and a draft postmortem for one incident.

Everything here is derived from what the engine recorded. It does not know the
root cause, only the class of trouble, and the postmortem says so.
"""
from datetime import timedelta
from statistics import mean, median

import pandas as pd

from backend.engine.notify import ACTION_WORDS, CHECK_FIRST, TITLES


def _mins(a, b):
    return (pd.Timestamp(b) - pd.Timestamp(a)) / timedelta(minutes=1)


def _trail(engine, inc):
    return [e for e in engine.audit if e.get("incident") == inc.id]


def incident_rows(engine, now):
    rows = []
    for inc in engine.incidents:
        trail = _trail(engine, inc)
        acts = [e for e in trail if e["type"] == "action" and e["ok"]]
        end = inc.closed or now
        rows.append(dict(
            id=inc.id, cause=inc.cause, target=inc.target, opened=inc.opened.isoformat(), resolved=inc.closed is not None,
            minutes_open=round(_mins(inc.opened, end), 1),
            minutes_to_first_action=round(_mins(inc.opened, acts[0]["ts"]), 1) if acts else None,
            actions=len(acts), verified=sum(e["type"] == "verified" for e in trail),
            handed_to_person=any(e["type"] == "escalated" for e in trail),
            alerted=any(e["type"] == "notified" and e.get("ok") for e in trail),
            fixed_by_engine=any(e["type"] == "incident_resolved" and e.get("how") == "after action" for e in trail)))
    return rows


def reliability(engine, now):
    """one summary for everything the engine has seen so far"""
    rows = incident_rows(engine, now)
    done = [r for r in rows if r["resolved"]]
    acted = [r["minutes_to_first_action"] for r in rows if r["minutes_to_first_action"] is not None]
    acts = sum(r["actions"] for r in rows)
    return dict(
        incidents=len(rows), open=len(rows) - len(done), resolved=len(done),
        fixed_by_engine=sum(r["fixed_by_engine"] for r in done),
        handed_to_person=sum(r["handed_to_person"] for r in rows),
        mean_minutes_open=round(mean(r["minutes_open"] for r in done), 1) if done else None,
        median_minutes_open=round(median(r["minutes_open"] for r in done), 1) if done else None,
        mean_minutes_to_first_action=round(mean(acted), 1) if acted else None,
        actions=acts, actions_verified=sum(r["verified"] for r in rows),
        alerts_sent=sum(1 for e in engine.audit if e["type"] == "notified" and e.get("ok")),
        simulated=engine.source.simulated)


def postmortem(engine, inc, now):
    """a markdown draft. the sections only a person can fill in are left as questions."""
    title = TITLES.get(inc.cause, inc.cause)
    where = f"the {inc.target} tier" if inc.scope == "tier" else inc.target
    trail = _trail(engine, inc)
    end = inc.closed or now
    out = [f"# Postmortem draft: {title} on {where}", ""]
    if engine.source.simulated:
        out += ["> SIMULATED incident. Nothing real happened.", ""]
    out += [f"- Started: {inc.opened:%Y-%m-%d %H:%M} (when the engine noticed it)",
            f"- {'Ended' if inc.closed else 'Still open, as of'}: {end:%Y-%m-%d %H:%M}",
            f"- Duration: {int(_mins(inc.opened, end))} minutes",
            f"- Severity: {inc.severity}", ""]
    out += ["## What the engine saw", ""] + [f"- {e}" for e in inc.evidence[:4]]
    if inc.change:
        out += ["", f"A deploy was seen {inc.change['minutes_before']} minutes before this started: `{inc.change['text']}`"]
    if inc.logs:
        out += ["", "Log lines that were new or louder than usual:", ""]
        out += [f"- `{c['template'][:120]}` ({'new' if c['new'] else str(c['in_window']) + ' in 30 min vs ' + str(c['in_baseline']) + ' in the day before'})"
                for c in inc.logs[:4]]
    out += ["", "## Timeline", ""]
    words = {"incident_opened": "Noticed", "action": "Ran", "verified": "Check passed", "verify_failed": "Check failed",
             "approved": "Approved", "rejected": "Rejected", "escalated": "Handed to a person", "rolled_back": "Undone",
             "notified": "Alert", "incident_resolved": "Closed", "reopened": "Came back", "decision": "Decided"}
    for e in trail:
        if e["type"] == "decision" and e.get("status") in ("none", "shadow") and e.get("action") == "observe":
            continue
        what = ACTION_WORDS.get(e.get("action"), "tell a person" if e.get("action") == "notify" else e.get("action") or "")
        extra = {"decision": f"{e.get('status')}: {what} ({'; '.join(e.get('reasons') or [])})", "action": what,
                 "verified": what, "verify_failed": what, "approved": f"{what}, by {e.get('by')}", "rejected": f"{what}, by {e.get('by')}",
                 "rolled_back": what, "notified": e.get("subject", ""), "incident_resolved": e.get("how", ""),
                 "incident_opened": e.get("cause", "")}.get(e["type"], "")
        out.append(f"- {e['ts'][11:16]} {words.get(e['type'], e['type'])}: {extra}".rstrip(": "))
    out += ["", "## What fixed it", ""]
    fixed = [e for e in trail if e["type"] == "verified"]
    if fixed:
        out.append("The engine's own fix: " + ", ".join(ACTION_WORDS.get(e["action"], e["action"]) for e in fixed) + ".")
    elif inc.closed:
        out.append("It cleared without a fix that the engine could confirm.")
    else:
        out.append("Not fixed yet.")
    out += ["", "## For a person to fill in", "",
            "- Root cause (the engine only knows the class of trouble, not why it happened)",
            f"- Customer or user impact. A starting point: {CHECK_FIRST.get(inc.cause, 'see the evidence above')}",
            "- What stops this happening again", ""]
    return "\n".join(out)
