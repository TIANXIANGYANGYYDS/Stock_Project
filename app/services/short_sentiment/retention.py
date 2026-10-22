"""Keep a whole, same-day observation; never synthesize individual quotes."""
from copy import deepcopy

from app.services.short_sentiment.facts import CN, instant
from app.services.short_sentiment.metrics import MIN_COVERAGE

NONTRADING_SESSIONS = {"lunch", "closing_auction", "closed_pending_final"}


def session_at(value):
    clock = instant(value).astimezone(CN).strftime("%H:%M:%S")
    if clock < "09:30:00":
        return "opening_auction"
    if clock < "11:30:00" or "13:00:00" <= clock < "14:57:00":
        return "continuous"
    if clock < "13:00:00":
        return "lunch"
    return "closing_auction" if clock < "15:00:00" else "closed_pending_final"


def usable_observation(snapshot):
    if not snapshot:
        return False
    meta = snapshot["meta"]
    coverage = snapshot["market"]["values"]["coverage"]
    return (not meta["is_final"] and not meta.get("is_retained", False)
            and coverage is not None and coverage >= MIN_COVERAGE
            and snapshot["market"]["values"]["eligible"] > 0)


def public_snapshot(observation, retained, *, session=None):
    """The processing checkpoint stays separate from this public view."""
    meta = observation["meta"]
    session = session or meta["session"]
    if meta["is_final"] or session not in NONTRADING_SESSIONS:
        return observation
    usable = usable_observation(observation)
    if usable and session == meta["session"]:
        return observation
    base = observation if usable else retained
    matches = base and all(base["meta"][key] == meta[key]
                           for key in ("scope", "trade_date", "classification_version"))
    if not matches or not usable_observation(base):
        result = deepcopy(observation)
        result["meta"].update(session=session, retention_reason="no_same_day_usable_observation")
        return result
    result = deepcopy(base)
    result["meta"].update(
        snapshot_id=meta["snapshot_id"], generated_at=meta["generated_at"], session=session,
        is_final=False, is_retained=True, data_mode="retained_observation",
        retention_reason="nontrading_invalid_observation" if not usable else "session_ended_without_new_observation",
        retained_from_snapshot_id=base.get("observation_id", base["meta"]["snapshot_id"]),
        processed_as_of=meta["as_of"], finalization=meta["finalization"],
        clock_offset_seconds=meta.get("clock_offset_seconds", 0), clock_basis=meta.get("clock_basis", "system"))
    for key in ("sequence", "input_id", "source_cursor", "series_minute"):
        result[key] = observation.get(key)
    # Events and minute series still come from the actual processing observation.
    result["events"] = observation["events"]
    return result
