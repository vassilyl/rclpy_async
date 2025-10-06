from __future__ import annotations
from contextlib import contextmanager, asynccontextmanager
import logging
import threading
from typing import Awaitable, Callable, List, Optional

import anyio
import anyio.from_thread
import anyio.to_thread


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


class NodeAsync(anyio.AsyncContextManagerMixin):
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
        """Create an asynchronous ROS node wrapper bound to an AnyIO portal.

        Parameters
        ----------
        portal : anyio.from_thread.BlockingPortal
            The portal that bridges executor callback threads with the AnyIO event loop.
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
        self._portal = portal
        self._node_name = node_name
        self._args = args
        self._namespace = namespace
        self._enable_rosout = enable_rosout
        self._start_parameter_services = start_parameter_services
        self._domain_id = domain_id
        self._reentrant_cbg = ReentrantCallbackGroup()
        self.node: Optional[Node] = None

    @asynccontextmanager
    async def __asynccontextmanager__(self):
        name = self._node_name or "(no name)"
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
        executor = MultiThreadedExecutor(context=context)
        executor.add_node(node)

        # start spinning thread
        spin_thread = threading.Thread(
            target=executor.spin, name=name + "_spin", daemon=True
        )
        spin_thread.start()
        try:
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
                    logger.debug(f"ROS node '{self._node_name}' shutdown complete")

    def _portal_cb(
        self, callback: Callable[..., None] | Callable[..., Awaitable[None]]
    ):
        def _cb(*args):
            try:
                self._portal.start_task_soon(callback, *args)
            except RuntimeError:
                # This portal is not running
                logger.debug(
                    "Runtime error in scheduling subscription callback.",
                    exc_info=True,
                )

        return _cb

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
        to call the ``async_callback``. Exiting the context destroys the subscription
        and stops processing of incoming messages.

        Note that exceptions raised by ``async_callback`` run inside an AnyIO task
        and are not propagated back to the caller of ``subscription``; handle errors
        within your callback (e.g., with try/except) to surface them appropriately.


        Parameters
        ----------
        msg_type : type
            The ROS message type class (e.g., std_msgs.msg.String).
        topic_name : str
            The name of the ROS topic to subscribe to (e.g., "/chat").
        async_callback : Callable[[object], Awaitable[None]] or Callable[[object], None]
            An async function to call with each incoming message.
        qos_profile : QoSProfile or int
            The QoS profile to use (e.g., 1 for default reliability).


        Returns
        -------
        AsyncContextManager
            An async context manager that destroys the subscription on exit.
        """
        if self.node is None:
            raise RuntimeError("ROS node is not initialized.")

        subscription = self.node.create_subscription(
            msg_type,
            topic_name,
            self._portal_cb(async_callback),
            qos_profile,
            callback_group=self._reentrant_cbg,
        )
        try:
            yield subscription
        finally:
            self.node.destroy_subscription(subscription)

    async def await_rclpy_future(self, fut: RclpyFuture, **kwargs):
        """Await completion of an rclpy Future from within the AnyIO event loop.

        The method registers a callback on ``fut`` that notifies the AnyIO loop
        when the ROS executor marks the future as done, then awaits that
        notification. If the future completes with an exception, the exception is
        raised; otherwise the resolved result is returned.

        Parameters
        ----------
        fut : rclpy.task.Future
            The rclpy future to wait on. It should originate from the executor
            associated with this node.
        **kwargs
            Present for API compatibility; currently unused.

        Returns
        -------
        Any
            The result stored in ``fut`` once it completes successfully.

        Raises
        ------
        Exception
            Re-raises any exception set on the future.
        """
        evt = anyio.Event()

        fut.add_done_callback(self._portal_cb(lambda _: evt.set()))
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
                    f"Service server '{srv_name}' not available within {server_wait_timeout}s"
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

        Note that exceptions raised by ``feedback_handler_async`` run inside an AnyIO task
        and are not propagated back to the caller of ``action_client``; handle errors
        within your callback (e.g., with try/except) to surface them appropriately.

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
                        feedback_callback=self._portal_cb(feedback_handler_async)
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
