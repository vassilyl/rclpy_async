from action_msgs.msg import GoalStatus

_goal_status_map = {
    getattr(GoalStatus, k): k[7:] for k in dir(GoalStatus) if k.startswith("STATUS_")
}


def goal_status_str(status: int) -> str:
    """Convert a GoalStatus code into a human-readable string."""
    return _goal_status_map.get(status, f"UNKNOWN({status})")

def goal_uuid_str(uuid_seq) -> str:
    """Convert a goal uuid to a string representation."""
    return "".join(f"{byte:02x}" for byte in uuid_seq)