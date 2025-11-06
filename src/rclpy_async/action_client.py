from contextlib import contextmanager
import logging
from typing import Awaitable, Callable

import anyio

from rclpy.node import Node
import rclpy.action
from action_msgs.srv import CancelGoal

import rclpy_async
from rclpy_async.utilities import goal_status_str, goal_uuid_str, server_ready

@contextmanager
def action_client(
    node: Node,
    action_type,
    action_name: str,
    *,
    server_wait_timeout: float = 5.0,
):
    """Async context manager for a ROS action client.

    Yields an async callable that sends a goal to the action server,
    waits for the action result, and returns a tuple of (status, result).
    The function also translates cancel scope cancellation to
    ROS action goal cancellation.

    By default exceptions in ``feedback_task`` are suppressed. Set
    ``propagate_feedback_exceptions=True`` to propagate exceptions
    to the caller's scope.

    Parameters
    ----------
    action_type : type
        The ROS action type class (e.g., example_interfaces.action.Fibonacci).
    action_name : str
        The name of the ROS action to call (e.g., "/fibonacci").
    feedback_task : Callable[[object], None] or Callable[[object], Awaitable[None]], optional
        An async function to handle feedback messages, by default None.
    server_wait_timeout : float, optional
        Time in seconds to wait for the action server to be available, by default 5 seconds.

    Returns
    -------
    AsyncContextManager
        An async context manager that yields a function to send goals to the action server.
    """
    logger = node.get_logger()
    action_client = rclpy.action.ActionClient(node, action_type, action_name)

    async def _call(
        goal_msg: object,
        feedback_task: Callable[[object], None]
        | Callable[[object], Awaitable[None]]
        | None = None,
    ) -> tuple[int, object]:
        if not await server_ready(action_client.server_is_ready, server_wait_timeout):
            raise TimeoutError(
                f"Action server '{action_name}' not available within {server_wait_timeout}s"
            )

        # Send goal and await goal handle
        goal_handle = None
        # Do not allow cancellation until we receive the goal handle
        with anyio.move_on_after(
            server_wait_timeout, shield=True
        ) as send_goal_scope:
            goal_future = action_client.send_goal_async(
                goal_msg,
                feedback_callback=feedback_task,
            )

            logger.debug(f"Sent goal to {action_name}, awaiting goal handle...")
            goal_handle = await rclpy_async.future_result(goal_future)
        if send_goal_scope.cancelled_caught:
            raise RuntimeError("Didn't receive goal handle before timeout.")

        if goal_handle is None or not goal_handle.accepted:
            raise RuntimeError("Action goal was rejected by the server.")

        # Await result; if cancelled, try to cancel the goal on server
        goal_uuid = (
            goal_uuid_str(goal_handle.goal_id.uuid)
            if logger.is_enabled_for(logging.DEBUG)
            else ""
        )
        result_future = goal_handle.get_result_async()

        logger.debug(f"Goal {goal_uuid} accepted, awaiting result...")
        try:
            result = await rclpy_async.future_result(result_future)
            if result is None:
                raise RuntimeError("Action result future returned None.")
            logger.debug(
                f"Goal {goal_uuid} {goal_status_str(result.status)} with {result.result}"
            )
            # result has .status and .result fields
            return (result.status, result.result)
        except anyio.get_cancelled_exc_class():
            # The scope was cancelled.
            # Request cancellation even if outer scope was cancelled
            with anyio.move_on_after(server_wait_timeout, shield=True):
                logger.debug(f"Cancelling goal {goal_uuid}...")
                cancel_future = goal_handle.cancel_goal_async()
                cancel_result = await rclpy_async.future_result(
                    cancel_future
                )
                if cancel_result is None or not isinstance(
                    cancel_result, CancelGoal.Response
                ):
                    logger.warning("Invalid cancel response.")
                elif cancel_result.goals_canceling:
                    goal_ids = [gi.goal_id for gi in cancel_result.goals_canceling]
                    goal_ids_str = (
                        [goal_uuid_str(id.uuid) for id in goal_ids]
                        if logger.is_enabled_for(logging.DEBUG)
                        else None
                    )
                    logger.debug(f"Cancelling goals: {goal_ids_str}.")
                    if goal_handle.goal_id in goal_ids:
                        logger.info("The action ACCEPTED cancellation of the goal.")
                    else:
                        logger.info("The action REJECTED cancellation of the goal.")
                # wait the action completes cancellation
                await rclpy_async.future_result(result_future)
            raise

    try:
        yield _call
    finally:
        action_client.destroy()
