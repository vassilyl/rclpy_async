from __future__ import annotations
from contextlib import contextmanager, asynccontextmanager
import inspect
import logging
import threading
from typing import Awaitable, Callable, List, Optional

import anyio
import anyio.from_thread
import anyio.to_thread


import rclpy
from rclpy.context import Context
from rclpy.qos import QoSProfile, qos_profile_services_default
from rclpy.node import Node
from rclpy.action import ActionClient, ActionServer
import rclpy.action.server
from rclpy.action.server import ServerGoalHandle as ActionServerGoalHandle
from rclpy.action.server import CancelResponse as ActionCancelResponse
from rclpy.action.server import GoalResponse as ActionGoalResponse
from action_msgs.srv import CancelGoal

from rclpy_async.utilities import goal_status_str, goal_uuid_str

from .executor import DualThreadExecutor

logger = logging.getLogger(__name__)


class NodeAsync(anyio.AsyncContextManagerMixin):
    def __init__(
        self,
        node_name: str,
        args: List[str] | None = None,
        namespace: Optional[str] = None,
        enable_rosout: bool = True,
        start_parameter_services: bool = True,
        domain_id: int | None = None,
    ):
        """Create an asynchronous ROS node wrapper bound to an AnyIO portal.

        Parameters
        ----------
        node_name : str
            Name of the ROS node to create. Must satisfy ROS 2 naming rules.
        args : list[str] | None, optional
            Command line arguments passed to ``rclpy.init``.
        namespace : str | None, optional
            Namespace prefix applied to the node when created.
        enable_rosout : bool, optional
            Whether to publish rosout logs for the node, defaults to True.
        start_parameter_services : bool, optional
            Whether to create parameter services (describe/get/set) for the node, defaults to True.
        domain_id : int | None, optional
            ROS domain identifier to join. If ``None`` the default domain is used.
        """
        self._node_name = node_name
        self._args = args
        self._namespace = namespace
        self._enable_rosout = enable_rosout
        self._start_parameter_services = start_parameter_services
        self._domain_id = domain_id
        self.node: Optional[Node] = None
        # the below belongs to anyio event loop, do not touch from other threads
        self._cancellation_scopes = {}

    @asynccontextmanager
    async def __asynccontextmanager__(self):
        name = self._node_name or "(no name)"
        # capture event loop to use from rclpy threads
        async with anyio.from_thread.BlockingPortal() as portal:
            self._portal = portal
            # internal task group for callbacks that require exception propagation
            async with anyio.create_task_group() as self._task_group:
                logger.debug(f"Starting node '{name}'")
                context = Context()
                rclpy.init(args=self._args, context=context, domain_id=self._domain_id)
                node = rclpy.create_node(
                    self._node_name,
                    context=context,
                    namespace=self._namespace,  # type: ignore  (invalid annotation in rclpy)
                    enable_rosout=self._enable_rosout,
                    start_parameter_services=self._start_parameter_services,
                )
                logger.debug(f"Created ROS node '{name}'")
                executor = DualThreadExecutor(
                    context=context, portal=portal, task_group=self._task_group
                )
                executor.add_node(node)
                # start spinning thread
                spin_thread = threading.Thread(
                    target=executor.spin, name=name + "_spin", daemon=True
                )
                spin_thread.start()
                try:
                    self._executor = executor
                    self.node = node
                    yield self
                finally:
                    logger.debug(f"Shutting down node '{self._node_name}'")
                    try:
                        executor.remove_node(node)

                        # shutdown blocks until work is complete,
                        # should not block event loop as work can be scheduled here
                        def _shutdown():
                            executor.shutdown()
                            spin_thread.join(timeout=1.0)

                        await anyio.to_thread.run_sync(_shutdown)
                        if spin_thread.is_alive():
                            logger.warning(
                                "Spin thread did not terminate in 1 sec,"
                                " some ROS work may not be complete yet."
                            )
                    finally:
                        try:
                            node.destroy_node()
                        finally:
                            context.shutdown()
                            logger.debug(
                                f"ROS node '{self._node_name}' shutdown complete"
                            )

    @contextmanager
    def publisher(
        self,
        msg_type,
        topic_name: str,
        qos_profile: QoSProfile | int,
    ):
        """
        Create a context manager to create a ROS topic publisher.

        While in the context, the publisher can be used to publish messages.
        Exiting the context destroys the publisher.

        Parameters
        ----------
        msg_type : type
            The ROS message type class (e.g., std_msgs.msg.String).
        topic_name : str
            The name of the ROS topic to publish to (e.g., "/chat").
        qos_profile : QoSProfile or int
            The QoS profile to use (e.g., 1 for default reliability).

        Returns
        -------
        ContextManager
            A context manager that destroys the publisher on exit.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        publisher = self.node.create_publisher(
            msg_type,
            topic_name,
            qos_profile,
        )
        try:
            yield publisher
        finally:
            self.node.destroy_publisher(publisher)

    @contextmanager
    def subscription(
        self,
        msg_type,
        topic_name,
        callback: Callable[[object], Awaitable[None]] | Callable[[object], None],
        qos_profile: QoSProfile | int,
    ):
        """
        Create a context manager to subscribe to a ROS topic.

        While in the context, each topic message starts a ``callback``
        in the AnyIO event loop. Exiting the context destroys the subscription
        and stops processing of incoming messages.

        By default exceptions in ``callback`` are suppressed. Set

        Parameters
        ----------
        msg_type : type
            The ROS message type class (e.g., std_msgs.msg.String).
        topic_name : str
            The name of the ROS topic to subscribe to (e.g., "/chat").
        callback : Callable[[object], Awaitable[None]] or Callable[[object], None]
            A function to call with each incoming message.
            The function will run in the AnyIO event loop.
        qos_profile : QoSProfile or int
            The QoS profile to use (e.g., 1 for default reliability).


        Returns
        -------
        ContextManager
            A context manager that destroys the subscription on exit.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        subscription = self.node.create_subscription(
            msg_type,
            topic_name,
            callback,  # type: ignore (invalid annotation in rclpy)
            qos_profile,
        )
        try:
            yield subscription
        finally:
            self.node.destroy_subscription(subscription)

    @contextmanager
    def service_client(
        self,
        srv_type,
        srv_name: str,
        *,
        qos_profile: QoSProfile = qos_profile_services_default,
        server_wait_timeout: float = 5.0,
    ):
        """Context manager for a ROS service client.

        Yields an async callable that sends a service request and waits for
        the server response.

        Parameters
        ----------
        srv_type : type
            The ROS service type class (e.g., std_srvs.srv.SetBool).
        srv_name : str
            The name of the ROS service to call (e.g., "/toggle").
        qos_profile : QoSProfile, optional
            The QoS profile to use for the service client, by default qos_profile_services_default.
        server_wait_timeout : float, optional
            Time in seconds to wait for the service server to be available, by default 5 seconds.

        Returns
        -------
        ContextManager
            A context manager that yields a function to call the service.
        """

        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")
        rlcpy_client = self.node.create_client(
            srv_type,
            srv_name,
            qos_profile=qos_profile,
        )

        async def _call(request):
            """The async function to call and await the service on AnyIO event loop."""
            server_ready = rlcpy_client.service_is_ready()
            if not server_ready:
                with anyio.move_on_after(server_wait_timeout):
                    while not server_ready:
                        await anyio.sleep(0.1)
                        server_ready = rlcpy_client.service_is_ready()
            if not server_ready:
                raise TimeoutError(
                    f"Service server '{srv_name}' not available within {server_wait_timeout}s"
                )

            fut = rlcpy_client.call_async(request)
            return await self._executor.rclpy_future_result(fut)

        try:
            yield _call
        finally:
            self.node.destroy_client(rlcpy_client)

    @contextmanager
    def service_server(
        self,
        srv_type,
        srv_name: str,
        callback_task: Callable[[object, object], Awaitable[object]]
        | Callable[[object, object], object],
        propagate_callback_exceptions: bool = False,
        qos_profile: QoSProfile = qos_profile_services_default,
    ):
        """
        Create a context manager to create a ROS service server.

        While in the context, each service request starts a ``callback_task``
        in the AnyIO event loop. Exiting the context destroys the service server
        and stops processing of incoming requests.

        Parameters
        ----------
        srv_type : type
            The ROS service type class (e.g., std_srvs.srv.SetBool).
        srv_name : str
            The name of the ROS service to create (e.g., "/toggle").
        callback_task : Callable[[object, object], Awaitable[object]] or Callable[[object, object], object]
            An async function to call with each incoming request.
        qos_profile : QoSProfile, optional
            The QoS profile to use for the service server, by default qos_profile_services_default.

        Returns
        -------
        ContextManager
            A context manager that destroys the service server on exit.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        service = self.node.create_service(
            srv_type, srv_name, callback_task, qos_profile=qos_profile
        )
        try:
            yield service
        finally:
            self.node.destroy_service(service)

    @contextmanager
    def action_client(
        self,
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
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")
        action_client = ActionClient(self.node, action_type, action_name)

        async def _call(
            goal_msg: object,
            feedback_task: Callable[[object], None]
            | Callable[[object], Awaitable[None]]
            | None = None,
        ) -> tuple[int, object]:
            # Wait for server
            server_ready = action_client.server_is_ready()
            if not server_ready:
                with anyio.move_on_after(server_wait_timeout):
                    while not server_ready:
                        await anyio.sleep(0.1)
                        server_ready = action_client.server_is_ready()
            if not server_ready:
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
                    feedback_callback=self._executor.to_task(feedback_task),
                )

                logger.debug(f"Sent goal to {action_name}, awaiting goal handle...")
                goal_handle = await self._executor.rclpy_future_result(goal_future)
            if send_goal_scope.cancelled_caught:
                raise RuntimeError("Didn't receive goal handle before timeout.")

            if goal_handle is None or not goal_handle.accepted:
                raise RuntimeError("Action goal was rejected by the server.")

            # Await result; if cancelled, try to cancel the goal on server
            goal_uuid = (
                goal_uuid_str(goal_handle.goal_id.uuid)
                if logger.isEnabledFor(logging.DEBUG)
                else ""
            )
            result_future = goal_handle.get_result_async()

            logger.debug(f"Goal {goal_uuid} accepted, awaiting result...")
            try:
                result = await self._executor.rclpy_future_result(result_future)
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
                    cancel_result = await self._executor.rclpy_future_result(
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
                            if logger.isEnabledFor(logging.DEBUG)
                            else None
                        )
                        logger.debug(f"Cancelling goals: {goal_ids_str}.")
                        if goal_handle.goal_id in goal_ids:
                            logger.info("The action ACCEPTED cancellation of the goal.")
                        else:
                            logger.info("The action REJECTED cancellation of the goal.")
                    # wait the action completes cancellation
                    await self._executor.rclpy_future_result(result_future)
                raise

        try:
            yield _call
        finally:
            try:
                action_client.destroy()
            except Exception:
                pass

    async def _execute_anyio_task(
        self,
        execute_future: rclpy.Future,
        execute: Callable[[ActionServerGoalHandle], Awaitable[object] | object],
        goal_handle: ActionServerGoalHandle,
    ):
        """The AnyIO task that runs the action goal execute callback.

        Creates a cancellation scope for the goal, runs the execute callback,
        and returns the result via execute_future.
        """
        goal_id = bytes(goal_handle.goal_id.uuid)
        cancel_scope = anyio.CancelScope()
        self._cancellation_scopes[goal_id] = cancel_scope
        try:
            with cancel_scope:
                if inspect.iscoroutinefunction(execute):
                    result = await execute(goal_handle)
                else:
                    result = execute(goal_handle)
                execute_future.set_result(result)
            if cancel_scope.cancelled_caught:
                execute_future.set_result(None)
        except Exception as e:
            execute_future.set_exception(e)
        finally:
            del self._cancellation_scopes[goal_id]

    def _wrap_execute(
        self,
        execute: Callable[[ActionServerGoalHandle], Awaitable[object] | object],
        empty_result: object,
    ):
        async def _call(
            goal_handle: ActionServerGoalHandle,
        ):
            # spin thread starts this callback
            execute_future = rclpy.Future()
            spawn_anyio_task = self._executor.to_task(self._execute_anyio_task)
            if spawn_anyio_task is not None:
                spawn_anyio_task(execute_future, execute, goal_handle)
                result = await execute_future
                if result is None:
                    goal_handle.canceled()
                    return empty_result
                else:
                    return result
            raise RuntimeError("Failed to spawn AnyIO task for action goal execution.")

        return _call

    async def _goal_anyio_task(
        self,
        rclpy_future: rclpy.Future,
        goal_callback: Callable[[object], Awaitable[bool]] | Callable[[object], bool],
        goal_handle: ActionServerGoalHandle,
    ):
        try:
            if inspect.iscoroutinefunction(goal_callback):
                result = await goal_callback(goal_handle)
            else:
                result = goal_callback(goal_handle)
            rclpy_future.set_result(
                ActionGoalResponse.ACCEPT if result else ActionGoalResponse.REJECT
            )
        except Exception as e:
            rclpy_future.set_exception(e)

    def _wrap_goal(
        self,
        goal_callback: Callable[[object], Awaitable[bool]] | Callable[[object], bool],
    ):
        async def _call(
            goal_handle: ActionServerGoalHandle,
        ):
            # spin thread starts this callback
            goal_future = rclpy.Future()
            spawn_anyio_task = self._executor.to_task(self._execute_anyio_task)
            if spawn_anyio_task is not None:
                spawn_anyio_task(goal_future, goal_callback, goal_handle)
                return await goal_future
            raise RuntimeError("Failed to spawn AnyIO task for action goal execution.")

        return _call

    def _cancellation_anyio_task(self, rclpy_future, goal_id):
        cancel_scope = self._cancellation_scopes.get(goal_id, None)
        if isinstance(cancel_scope, anyio.CancelScope):
            cancel_scope.cancel()
            rclpy_future.set_result(ActionCancelResponse.ACCEPT)
        else:
            rclpy_future.set_result(ActionCancelResponse.REJECT)

    async def _cancellation_rclpy_task(self, goal_handle):
        goal_id = bytes(goal_handle.goal_id.uuid)
        cancellation_future = rclpy.Future()
        spawn_anyio_task = self._executor.to_task(self._cancellation_anyio_task)
        if spawn_anyio_task is not None:
            spawn_anyio_task(cancellation_future, goal_id)
            return await cancellation_future
        return ActionCancelResponse.REJECT

    @contextmanager
    def action_server(
        self,
        action_type,
        action_name: str,
        execute_callback: Callable[[object], Awaitable[object]]
        | Callable[[object], object],
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
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        action_server = ActionServer(
            self.node,
            action_type,
            action_name,
            self._wrap_execute(execute_callback, action_type.Result()),
            goal_callback=None
            if goal_callback is None
            else self._wrap_goal(goal_callback),
            cancel_callback=self._cancellation_rclpy_task
            if accept_cancellations
            else rclpy.action.server.default_cancel_callback,
            goal_service_qos_profile=goal_service_qos_profile,
            result_service_qos_profile=result_service_qos_profile,
            cancel_service_qos_profile=cancel_service_qos_profile,
            feedback_pub_qos_profile=feedback_pub_qos_profile,
            status_pub_qos_profile=status_pub_qos_profile,
            result_timeout=result_timeout,
        )
        try:
            yield action_server
        finally:
            action_server.destroy()

    def get_logger(self):
        """Get the logger for this node.

        Returns
        -------
        logging.Logger
            The logger instance associated with this node.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")
        return self.node.get_logger()
