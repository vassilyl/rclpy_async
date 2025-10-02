from action_msgs.msg import GoalStatus

_goal_status_map = { getattr(GoalStatus, k): k[7:] for k in dir(GoalStatus) if k.startswith("STATUS_") }
def goal_status_str(status: int) -> str:
    return _goal_status_map.get(status, f"UNKNOWN({status})")
