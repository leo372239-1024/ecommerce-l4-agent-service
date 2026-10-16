"""Frozen scripted customer replies, independent of hidden scoring outcomes."""


def scripted_decision(case, proposal, aliases):
    script = case["scripted_user"]
    allowed = script["approval"] == "confirm_and_approve" and any(
        aliases.get(intent["order"]) == proposal["order_id"]
        and intent["action"] == proposal["action"]
        for intent in script.get("allowed_write_intents", [])
    )
    return "confirm" if allowed else "reject"
