import anyio
import rclpy

from rclpy_async.nodeasync import NodeAsync
from rclpy_async.async_executor import start_xtor
from rclpy_async.utilities import goal_status_str, goal_uuid_str


async def future_result(fut: rclpy.Future):
    """
    Await a future and return its result.

    :param fut: The future to await.
    :return: The result of the future.
    """
    if not fut.done():
        evt = anyio.Event()
        fut.add_done_callback(lambda f: evt.set())
        await evt.wait()
    return fut.result()


async def wait_for_action_server(
    action_client,
    server_wait_timeout: float = 5.0,
    polling_interval: float = 0.1,
) -> bool:
    """
    Wait for an action server to be available.

    :param action_client: The action client to check.
    :param server_wait_timeout: Time in seconds to wait for the server (default: 5.0).
    :param polling_interval: Time in seconds between availability checks (default: 0.1).
    :return: True if server is available, False if timeout.
    """
    server_ready = action_client.server_is_ready()
    if not server_ready:
        with anyio.move_on_after(server_wait_timeout):
            while not server_ready:
                await anyio.sleep(polling_interval)
                server_ready = action_client.server_is_ready()
    return server_ready


__all__ = [
    "NodeAsync",
    "start_xtor",
    "goal_status_str",
    "goal_uuid_str",
    "future_result",
    "wait_for_action_server",
]
