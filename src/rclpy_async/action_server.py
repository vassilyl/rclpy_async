from typing import Awaitable, Callable, Optional
from contextlib import contextmanager
import inspect
import anyio
from rclpy.action import ActionServer
import rclpy.action.server
from rclpy.action.server import ServerGoalHandle as GoalHandle
from rclpy.action.server import CancelResponse

import rclpy
import rclpy.node


def _handle_accepted_cancellable(goal_handle: GoalHandle):
    """Handle goal acceptance with cancellation support."""
    cancel_scope = anyio.CancelScope()
    setattr(goal_handle, "_anyio_cancel_scope", cancel_scope)
    rclpy.action.server.default_handle_accepted_callback(goal_handle)


def _cancel_goal_cancellable(goal_handle: GoalHandle):
    """Handle goal cancellation with cancellation support."""
    cancel_scope = getattr(goal_handle, "_anyio_cancel_scope", None)
    if isinstance(cancel_scope, anyio.CancelScope):
        cancel_scope.cancel()
    return CancelResponse.ACCEPT


def _wrap_execute_cancellable(
    callback: Callable[[GoalHandle], Awaitable[object]]
    | Callable[[GoalHandle], object],
    default_result: object
):
    if inspect.iscoroutinefunction(callback):

        async def _execute_async_cancellable(goal_handle: GoalHandle):
            """Execute goal with cancellation support."""
            cancel_scope = getattr(goal_handle, "_anyio_cancel_scope", None)
            if isinstance(cancel_scope, anyio.CancelScope):
                with cancel_scope:
                    return await callback(goal_handle)
                if cancel_scope.cancelled_caught:
                    goal_handle.canceled()
                    return default_result
            else:
                return await callback(goal_handle)

        return _execute_async_cancellable
    else:

        def _execute_sync_cancellable(goal_handle: GoalHandle):
            """Execute goal with cancellation support."""
            cancel_scope = getattr(goal_handle, "_anyio_cancel_scope", None)
            if isinstance(cancel_scope, anyio.CancelScope):
                with cancel_scope:
                    return callback(goal_handle)
                if cancel_scope.cancelled_caught:
                    goal_handle.canceled()
                    return default_result
            else:
                return callback(goal_handle)

        return _execute_sync_cancellable


@contextmanager
def action_server(
    node: rclpy.node.Node,
    action_type,
    action_name: str,
    execute_callback: Callable[[GoalHandle], Awaitable[object]]
    | Callable[[GoalHandle], object],
    *,
    goal_callback: Optional[
        Callable[[object], Awaitable[bool]] | Callable[[object], bool]
    ] = None,
    accept_cancellations: bool = True,
    goal_service_qos_profile=rclpy.action.server.qos_profile_services_default,
    result_service_qos_profile=rclpy.action.server.qos_profile_services_default,
    cancel_service_qos_profile=rclpy.action.server.qos_profile_services_default,
    feedback_pub_qos_profile=rclpy.action.server.QoSProfile(depth=10),
    status_pub_qos_profile=rclpy.action.server.qos_profile_action_status_default,
    result_timeout=900,
):
    """
    Create a context manager to create a ROS action server.

    While in the context, each goal request starts an ``execute_callback``
    in the AnyIO event loop. Exiting the context destroys the action server
    and stops processing of incoming goals.

    Parameters
    ----------
    action_type : type
        The ROS action type class (e.g., example_interfaces.action.Fibonacci).
    action_name : str
        The name of the ROS action to create (e.g., "/fibonacci").
    execute_callback : Callable[[object], Awaitable[object]] or Callable[[object], object]
        An async function to call with each incoming goal.
    goal_callback : Callable[[object], Awaitable[bool]] or Callable[[object], bool], optional
        An optional async function to call to accept/reject incoming goals.
    accept_cancellations : bool, optional
        Whether to accept cancellation requests from clients, by default True.
    goal_service_qos_profile : QoSProfile, optional
        The QoS profile to use for the goal service, by default qos_profile_services_default.
    result_service_qos_profile : QoSProfile, optional
        The QoS profile to use for the result service, by default qos_profile_services_default.
    cancel_service_qos_profile : QoSProfile, optional
        The QoS profile to use for the cancel service, by default qos_profile_services_default.
    feedback_pub_qos_profile : QoSProfile, optional
        The QoS profile to use for the feedback publisher, by default a depth 10 profile.
    status_pub_qos_profile : QoSProfile, optional
        The QoS profile to use for the status publisher, by default qos_profile_action_status_default.
    result_timeout : int, optional
        How long in seconds a result is kept by the server after a goal
        reaches a terminal state.
    Returns
    -------
    ContextManager
        A context manager that destroys the action server on exit.
    """
    action_server = (
        ActionServer(
            node,
            action_type,
            action_name,
            _wrap_execute_cancellable(execute_callback, action_type.Result()),
            goal_callback=goal_callback,
            handle_accepted_callback=_handle_accepted_cancellable,
            cancel_callback=_cancel_goal_cancellable,
            goal_service_qos_profile=goal_service_qos_profile,
            result_service_qos_profile=result_service_qos_profile,
            cancel_service_qos_profile=cancel_service_qos_profile,
            feedback_pub_qos_profile=feedback_pub_qos_profile,
            status_pub_qos_profile=status_pub_qos_profile,
            result_timeout=result_timeout,
        )
        if accept_cancellations
        else ActionServer(
            node,
            action_type,
            action_name,
            execute_callback,
            goal_callback=goal_callback,
            goal_service_qos_profile=goal_service_qos_profile,
            result_service_qos_profile=result_service_qos_profile,
            cancel_service_qos_profile=cancel_service_qos_profile,
            feedback_pub_qos_profile=feedback_pub_qos_profile,
            status_pub_qos_profile=status_pub_qos_profile,
            result_timeout=result_timeout,
        )
    )
    try:
        yield action_server
    finally:
        action_server.destroy()
