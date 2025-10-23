from __future__ import annotations
from typing import Awaitable, Callable

import anyio
import anyio.abc
import anyio.from_thread


from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor, ExternalShutdownException
from rclpy.task import Future as RclpyFuture

import inspect


class DualThreadExecutor(SingleThreadedExecutor):
    """An rclpy SingleThreadedExecutor that swallows ExternalShutdownException."""

    def __init__(
        self,
        *,
        context: Context,
        portal: anyio.from_thread.BlockingPortal,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        super().__init__(context=context)
        self._portal = portal
        self._task_group = task_group

    @staticmethod
    def _call_async(fn):
        """Async adaptor to a regular function."""

        async def _wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        return _wrapper

    def to_task(self, fn: Callable[..., None] | Callable[..., Awaitable[None]] | None):
        """Adapt a callback to run in the executor's task group."""
        if fn is None:
            return None
        callback_async = fn if inspect.iscoroutinefunction(fn) else self._call_async(fn)

        def _callable(*args, **kwargs):
            return self._portal.start_task_soon(
                self._task_group.start_soon,
                callback_async,
                *args,
                **kwargs,
            )

        return _callable

    async def _execute_subscription_anyio(self, sub, msg):
        (
            await sub.callback(msg)
            if inspect.iscoroutinefunction(sub.callback)
            else sub.callback(msg)
        )

    async def _execute_subscription(self, sub, msg):
        if msg is not None:
            callback = self.to_task(sub.callback)
            if callback is not None:
                callback(msg)

    @staticmethod
    async def _execute_service_anyio(srv, request, header):
        response_template = srv.srv_type.Response()
        response = (
            await srv.callback(request, response_template)
            if inspect.iscoroutinefunction(srv.callback)
            else srv.callback(request, response_template)
        )
        srv.send_response(response, header)

    async def _execute_service(self, srv, request_and_header):
        if request_and_header is None:
            return
        (request, header) = request_and_header
        if request:
            execute_service_anyio = self.to_task(self._execute_service_anyio)
            if execute_service_anyio is not None:
                execute_service_anyio(srv, request, header)

    def spin(self):
        try:
            super().spin()
        except ExternalShutdownException:
            pass

    async def rclpy_future_result(self, fut: RclpyFuture):
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
        fut.add_done_callback(self.to_task(lambda _: evt.set()))
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
