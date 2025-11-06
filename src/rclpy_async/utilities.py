from typing import Callable

import anyio
import rclpy
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


async def server_ready(
    server_ready: Callable[[], bool],
    server_wait_timeout: float = 5.0,
    polling_interval: float = 0.1,
) -> bool:
    """Await availability of a ROS server, polling until timeout."""

    ready = server_ready()
    if not ready:
        with anyio.move_on_after(server_wait_timeout):
            while not ready:
                await anyio.sleep(polling_interval)
                ready = server_ready()
    return ready


async def future_result(fut: rclpy.Future):
    """Await a future and return its result."""
    if not fut.done():
        evt = anyio.Event()
        fut.add_done_callback(lambda f: evt.set())
        await evt.wait()
    return fut.result()
