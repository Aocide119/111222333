"""Emit a final-turn reminder from the core's measured budget state."""


def execute(payload, context):
    return {
        "reminder": "The interaction is ending because of "
        + str(payload.get("reason", "the configured budget"))
        + ". Answer using evidence already delivered; state missing evidence clearly."
    }
