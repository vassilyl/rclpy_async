from __future__ import annotations
from contextlib import contextmanager, asynccontextmanager
import logging
import threading
from typing import Awaitable, Callable, List, Optional

import anyio
import anyio.from_thread


import rclpy
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSProfile, qos_profile_services_default
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.task import Future as RclpyFuture

from rclpy_async.utilities import goal_status_str, goal_uuid_str


logger = logging.getLogger(__name__)

# To interoperate with async frameworks rclpy executor callback threads
# must communicate with the event loop.
# Functions in `anyio.from_thread` can only be called from anyio worker threads
# as other threads do not have access to the default event loop context.
# The solution is to create a BlockingPortal pass it to NodeAsync.
# The blocking portal is able to pass async tasks to an anyio event loop.


class NodeAsync(anyio.ContextManagerMixin):
    """
    Manages rclpy init/shutdown and runs an Executor in a background thread so AnyIO can drive the app.

    Usage:
        async with BlockingPortal() as portal:
            with NodeAsync("anyio_action_node") as app:
                # Use app helper methods or app.node directly
    """

    def __init__(
        self,
        portal: anyio.from_thread.BlockingPortal,
        node_name: str,
        args: List[str] | None = None,
        namespace: Optional[str] = None,
        enable_rosout: bool = True,
        start_parameter_services: bool = True,
        domain_id: int | None = None,
    ):
        # node_name must conform to ROS 2 naming rules:
        #  - must be 1-255 characters long
        #  - must only contain alphanumeric characters and underscores (a-z|A-Z|0-9|_)
        #  - must not start with a number
        #
        # domain_id must be in the range 0-101.
        self._portal = portal
        self._node_name = node_name
        self._args = args
        self._namespace = namespace
        self._enable_rosout = enable_rosout
        self._start_parameter_services = start_parameter_services
        self._domain_id = domain_id
        self._reentrant_cbg = ReentrantCallbackGroup()
        self.node: Optional[Node] = None

    @contextmanager
    def __contextmanager__(self):
        try:
            self.start()
            yield self
        finally:
            self.shutdown()

    def start(self):
        logger.debug(f"Starting node '{self._node_name}'")
        self._context = Context()
        rclpy.init(args=self._args, context=self._context, domain_id=self._domain_id)
        self.node = rclpy.create_node(
            self._node_name,
            context=self._context,
            namespace=self._namespace,  # type: ignore  (invalid annotation in rclpy)
            enable_rosout=self._enable_rosout,
            start_parameter_services=self._start_parameter_services,
        )
        logger.debug(f"Created ROS node '{self._node_name}'")
        self._executor = MultiThreadedExecutor(context=self._context)
        self._executor.add_node(self.node)

        # start spinning thread
        self._spin_thread = threading.Thread(
            target=self._executor.spin, name=self._node_name+"_spin", daemon=True
        )
        self._spin_thread.start()

    def shutdown(self):
        if self.node is None:
            logger.debug(f"Node '{self._node_name}' is not running")
            return
        logger.debug(f"Shutting down node '{self._node_name}'")
        try:
            executor = self._executor
            if executor is not None:
                executor.remove_node(self.node)
                executor.shutdown()
                self._spin_thread.join(timeout=5.0)
                if self._spin_thread.is_alive():
                    logger.warning("Spin thread did not terminate")
                self._executor = None
        finally:
            try:
                if self.node is not None:
                    self.node.destroy_node()
                    self.node = None
            finally:
                rclpy.shutdown(context=self._context)  # type: ignore  (invalid annotation in rclpy)
                self._context = None
                logger.debug(f"ROS node '{self._node_name}' shutdown complete")

    @contextmanager
    def subscription(
        self,
        msg_type,
        topic_name,
        async_callback: Callable[[object], Awaitable[None]] | Callable[[object], None],
        qos_profile: QoSProfile | int,
    ):
        """
        Create a context manager to subscribe to a ROS topic.

        While in the context, each message registers a task in the AnyIO event loop
        to call the async_callback. Exiting the context destroys the subscription
        and stops processing of incoming messages.


        Parameters
        ----------
        msg_type : type
            The ROS message type class (e.g., std_msgs.msg.String).
        topic_name : str
            The name of the ROS topic to subscribe to (e.g., "/chat").
        async_callback : Callable[[object], Awaitable[None]] or Callable[[object], None]
            An async function to call the with each incoming message.
        qos_profile : QoSProfile or int
            The QoS profile to use (e.g., 1 for default reliability).


        Returns
        -------
        AsyncContextManager
            An async context manager that destroys the subscription on exit.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        def _cb(msg):
            try:
                self._portal.start_task_soon(async_callback, msg)
            except RuntimeError as e:
                # This portal is not running?
                logger.debug(
                    f"Runtime error in scheduling subscription callback: {e}",
                    exc_info=True,
                )

        subscription = self.node.create_subscription(
            msg_type,
            topic_name,
            _cb,
            qos_profile,
            callback_group=self._reentrant_cbg,
        )
        try:
            yield subscription
        finally:
            subscription.destroy()

    async def await_rclpy_future(self, fut: RclpyFuture, **kwargs):
        evt = anyio.Event()

        def _cb(_):
            try:
                self._portal.start_task_soon(evt.set)
            except RuntimeError:
                # This portal is not running
                pass

        fut.add_done_callback(_cb)
        if fut.done():
            exc = fut.exception()
            if exc:
                raise exc
            return fut.result()
        await evt.wait()
        exc = fut.exception()
        if exc:
            raise exc
        return fut.result()

    @asynccontextmanager
    async def service_client(
        self,
        srv_type,
        srv_name: str,
        *,
        qos_profile: QoSProfile = qos_profile_services_default,
        server_wait_timeout: float = 5.0,
    ):
        """Create an async context manager for a ROS service client.

        The context manager yields an async function that takes a service request
        message, calls the ROS service and returns the response message.

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
        AsyncContextManager
            An async context manager that yields a function to call the service.
        """

        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")
        rlcpy_client = self.node.create_client(
            srv_type,
            srv_name,
            callback_group=self._reentrant_cbg,
            qos_profile=qos_profile,
        )

        try:
            # Wait for server
            server_ready = rlcpy_client.service_is_ready()
            if not server_ready:
                with anyio.move_on_after(server_wait_timeout):
                    while not server_ready:
                        await anyio.sleep(0.1)
                        server_ready = rlcpy_client.service_is_ready()
            if not server_ready:
                raise TimeoutError(
                    f"Action server '{srv_name}' not available within {server_wait_timeout}s"
                )

            async def _call(request):
                fut = rlcpy_client.call_async(request)
                return await self.await_rclpy_future(fut)

            yield _call
        finally:
            self.node.destroy_client(rlcpy_client)

    @asynccontextmanager
    async def action_client(
        self,
        action_type,
        action_name: str,
        *,
        feedback_handler_async: Callable[[object], None]
        | Callable[[object], Awaitable[None]]
        | None = None,
        server_wait_timeout: float = 5.0,
    ):
        """Create an async context manager for a ROS action client.

        The context manager yields an async function that takes a goal message,
        sends the goal to the action server, and returns a tuple of (status, result message).
        The function also translates cancel scope cancellation to action goal cancellation.

        Parameters
        ----------
        action_type : type
            The ROS action type class (e.g., example_interfaces.action.Fibonacci).
        action_name : str
            The name of the ROS action to call (e.g., "/fibonacci").
        feedback_handler_async : Callable[[object], None] or Callable[[object], Awaitable[None]], optional
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
        action_client = ActionClient(
            self.node, action_type, action_name, callback_group=self._reentrant_cbg
        )
        try:
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

            async def _call(goal_msg: object) -> tuple[int, object]:
                # Send goal and await goal handle
                goal_handle = None
                # Do not allow cancellation until we receive the goal handle
                with anyio.move_on_after(
                    server_wait_timeout, shield=True
                ) as send_goal_scope:
                    goal_future = action_client.send_goal_async(
                        goal_msg,
                        feedback_callback=lambda feedback_msg: self._portal.start_task_soon(
                            feedback_handler_async, feedback_msg
                        )
                        if feedback_handler_async is not None
                        else None,
                    )

                    logger.debug(f"Sent goal to {action_name}, awaiting goal handle...")
                    goal_handle = await self.await_rclpy_future(goal_future)
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
                    result = await self.await_rclpy_future(result_future)
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
                        cancel_result = await self.await_rclpy_future(cancel_future)
                        if cancel_result is None:
                            logger.warning("Cancel request future returned None.")
                        elif cancel_result.goals_canceling:
                            goal_ids = [
                                gi.goal_id for gi in cancel_result.goals_canceling
                            ]
                            goal_ids_str = (
                                [goal_uuid_str(id.uuid) for id in goal_ids]
                                if logger.isEnabledFor(logging.DEBUG)
                                else None
                            )
                            logger.debug(f"Cancelling goals: {goal_ids_str}.")
                            if goal_handle.goal_id in goal_ids:
                                logger.info(
                                    "The action ACCEPTED cancellation of the goal."
                                )
                            else:
                                logger.info(
                                    "The action REJECTED cancellation of the goal."
                                )
                        # wait the action completes cancellation
                        await self.await_rclpy_future(result_future)
                    raise

            yield _call
        finally:
            try:
                action_client.destroy()
            except Exception:
                pass
