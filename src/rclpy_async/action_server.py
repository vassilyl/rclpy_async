from typing import Awaitable, Callable, Optional
from contextlib import contextmanager
import inspect
import anyio
from rclpy.action import ActionServer
import rclpy.action.server
from rclpy.action.server import ServerGoalHandle as GoalHandle
from rclpy.action.server import CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup


import rclpy
import rclpy.node

reentrant_callback_group = ReentrantCallbackGroup()


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
    callback: Callable[[GoalHandle], Awaitable[object]],
    default_result: object,
):
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


@contextmanager
def action_server(
    node: rclpy.node.Node,
    action_type,
    action_name: str,
    execute_callback: Callable[[GoalHandle], Awaitable[object]]
    | Callable[[GoalHandle], object],
    *,
    callback_group=reentrant_callback_group,
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
    """Yield a cancellable ROS 2 action server bound to the given node.

    While inside the context, each goal request invokes ``execute_callback``.
    Exiting the context destroys the server and stops processing new goals.

    Parameters
    ----------
    node : rclpy.node.Node
        Node used to construct the underlying :class:`rclpy.action.ActionServer`.
    action_type : type
        ROS action type (for example ``example_interfaces.action.Fibonacci``).
    action_name : str
        Fully qualified name of the action to expose.
    execute_callback : Callable[[GoalHandle], Awaitable[object]] | Callable[[GoalHandle], object]
        Function executed for each accepted goal. If ``accept_cancellations`` is
        ``True`` and the callback is async, cancellation from clients is
        propagated into the AnyIO cancel scope.
    callback_group : CallbackGroup, optional
        Callback group used by the action server. Defaults to a shared
        :class:`ReentrantCallbackGroup`.
    goal_callback : Callable[[object], Awaitable[bool]] | Callable[[object], bool], optional
        Predicate that approves or rejects incoming goals before execution.
    accept_cancellations : bool, optional
        Whether client cancellation requests should propagate to the execute
        callback. Defaults to ``True``.
    goal_service_qos_profile : QoSProfile, optional
        QoS profile applied to the goal service (default:
        ``qos_profile_services_default``).
    result_service_qos_profile : QoSProfile, optional
        QoS profile applied to the result service (default:
        ``qos_profile_services_default``).
    cancel_service_qos_profile : QoSProfile, optional
        QoS profile applied to the cancel service (default:
        ``qos_profile_services_default``).
    feedback_pub_qos_profile : QoSProfile, optional
        QoS profile for the feedback publisher (default: depth-10 profile).
    status_pub_qos_profile : QoSProfile, optional
        QoS profile for the status publisher (default:
        ``qos_profile_action_status_default``).
    result_timeout : int, optional
        Seconds to retain results after reaching a terminal state (default: 900).

    Yields
    ------
    ActionServer
        The instantiated action server. It is destroyed automatically on exit.
    """
    wrapped_goal_callback = (
        None
        if goal_callback is None
        else lambda goal_msg: GoalResponse.ACCEPT
        if goal_callback(goal_msg)
        else GoalResponse.REJECT
    )
    action_server = (
        ActionServer(
            node,
            action_type,
            action_name,
            _wrap_execute_cancellable(execute_callback, action_type.Result()),
            callback_group=callback_group,
            goal_callback=wrapped_goal_callback,
            handle_accepted_callback=_handle_accepted_cancellable,
            cancel_callback=_cancel_goal_cancellable,
            goal_service_qos_profile=goal_service_qos_profile,
            result_service_qos_profile=result_service_qos_profile,
            cancel_service_qos_profile=cancel_service_qos_profile,
            feedback_pub_qos_profile=feedback_pub_qos_profile,
            status_pub_qos_profile=status_pub_qos_profile,
            result_timeout=result_timeout,
        )
        if accept_cancellations and inspect.iscoroutinefunction(execute_callback)
        else ActionServer(
            node,
            action_type,
            action_name,
            execute_callback,
            callback_group=callback_group,
            goal_callback=wrapped_goal_callback,
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
