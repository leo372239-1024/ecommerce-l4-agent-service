"""Projection pruning uses publisher contracts, never untrusted tool descriptions."""


def projection_candidates(registry, effect=None):
    return [
        spec.id
        for spec in registry
        if spec.risk == "read" and (effect is None or spec.effect == effect)
    ]


def bound_projection(query, order_id, registry):
    from app.business.intents import exact_effect

    effect = exact_effect(query)
    candidates = projection_candidates(registry, effect) if effect else []
    return (candidates[0], {"order_id": order_id}) if len(candidates) == 1 else None
