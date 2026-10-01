"""Unambiguous session identity in a Temporal namespace."""


def session_workflow_id(system, thread):
    return f"ua:{len(system)}:{system}:{thread}"
