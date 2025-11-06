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
    """Yield a coroutine helper for interacting with a ROS 2 action server.

    The yielded coroutine submits a goal to ``action_name``, waits for the final
    result, and returns ``(status, result)``. Cancelling the surrounding AnyIO
    scope requests goal cancellation on the server before re-raising the
    cancellation.

    Parameters
    ----------
    node : Node
        Node used to construct the underlying :class:`rclpy.action.ActionClient`.
    action_type : type
        ROS action type (for example ``example_interfaces.action.Fibonacci``).
    action_name : str
        Fully qualified name of the action to call.
    server_wait_timeout : float, optional
        Seconds to wait for the action server to become available (default: 5.0).

    Yields
    ------
    Callable[[object, Callable[[object], None] | Callable[[object], Awaitable[None]] | None],
              Awaitable[tuple[int, object]]]
        Coroutine that sends a goal, optionally processes feedback, and resolves
        with the goal status and result object.

    Raises
    ------
    TimeoutError
        If the action server does not report ready within ``server_wait_timeout``
        seconds.
    RuntimeError
        If the goal handle is not received in time or the server rejects the goal.
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
