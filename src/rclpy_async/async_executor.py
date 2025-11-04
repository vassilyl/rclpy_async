"""
AsyncExecutor class is a re-implementation of executors.SingleThreadedExecutor
with the intention of running all async callbacks in an anyio task group.

Instead of traditional boilerplate:

   ctx = rclpy.Context()
   rclpy.init(context=ctx)
   node = rclpy.create_node('my_node', context=ctx)
   # ... add node servers, clients, subscribers etc.
   executor = rclpy.executors.SingleThreadedExecutor()
   executor.add_node(node)
   executor.spin()
   rclpy.shutdown(context=ctx)

With the AsyncExecutor, the equivalent code becomes:

   ctx = rclpy.Context()
   rclpy.init(context=ctx)
   node = rclpy.create_node('my_node', context=ctx)
   # ... add node servers, clients, subscribers etc.
   async with AsyncExecutor() as executor:
       executor.add_node(node)
       await anyio.sleep_forever()

On entering the async context, AsyncExecutor creates anyio task group
and spawns its own implementation of wait spinning loop in a worker thread.
The loop just dispatches all callbacks to the task group for execution.

The class doesn't inherit from rclpy.executors.Executor, but should be able
to handle standard nodes and awaitables.
"""

import inspect
import threading
from contextlib import ExitStack, asynccontextmanager
from threading import RLock
from typing import (
    Awaitable,
    Callable,
    List,
    Optional,
    Set,
    TYPE_CHECKING,
)

import anyio
import anyio.from_thread

import rclpy
from rclpy.client import Client
from rclpy.clock import Clock
from rclpy.clock import ClockType
from rclpy.context import Context
from rclpy.exceptions import InvalidHandle
from rclpy.executors import await_or_execute
from rclpy.guard_condition import GuardCondition
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.service import Service
from rclpy.signals import SignalHandlerGuardCondition
from rclpy.subscription import Subscription
from rclpy.timer import Timer
from rclpy.utilities import get_default_context
from rclpy.utilities import timeout_sec_to_nsec
from rclpy.waitable import NumberOfEntities, Waitable

if TYPE_CHECKING:
    from typing import Type
    from rclpy.node import Node


class AsyncExecutor(anyio.AsyncContextManagerMixin):
    """
    Executor that runs callbacks in an anyio task group.

    This executor re-implements SingleThreadedExecutor to work with async/await
    patterns using anyio. It runs a wait loop in a background thread and dispatches
    all callbacks to an anyio task group for execution.
    """

    def __init__(
        self, *, context: Optional[Context] = None, wait_timeout_sec: float = 0.1
    ) -> None:
        """
        Initialize the AsyncExecutor.

        :param context: The context to be associated with, or None for the default context.
        :param wait_timeout_sec: Timeout for wait loop iterations in seconds (default: 0.1).
        """
        if anyio is None:
            raise ImportError(
                "anyio is required to use AsyncExecutor. "
                "Install it with: pip install anyio"
            )

        self._context = get_default_context() if context is None else context
        self._nodes: Set["Node"] = set()
        self._nodes_lock = RLock()

        # Guard condition to trigger wait set rebuild
        self._guard: Optional[GuardCondition] = GuardCondition(
            callback=None, callback_group=None, context=self._context
        )

        # Flag to indicate shutdown
        self._is_shutdown = False
        self._shutdown_lock = threading.Lock()

        # Clock for timing operations
        self._clock = Clock(clock_type=ClockType.STEADY_TIME)

        # Signal handler guard condition
        self._sigint_gc: Optional[SignalHandlerGuardCondition] = (
            SignalHandlerGuardCondition(context)
        )

        # Register shutdown callback
        self._context.on_shutdown(self.wake)

        # Wait timeout for spin loop
        self._wait_timeout_sec = wait_timeout_sec

    @property
    def context(self) -> Context:
        """Get the context associated with the executor."""
        return self._context

    def add_node(self, node: "Node") -> bool:
        """
        Add a node whose callbacks should be managed by this executor.

        :param node: The node to add to the executor.
        :return: True if the node was added, False otherwise.
        """
        with self._nodes_lock:
            if node not in self._nodes:
                self._nodes.add(node)
                node.executor = self
                # Rebuild the wait set so it includes this new node
                if self._guard:
                    self._guard.trigger()
                return True
            return False

    def remove_node(self, node: "Node") -> None:
        """
        Stop managing this node's callbacks.

        :param node: The node to remove from the executor.
        """
        with self._nodes_lock:
            try:
                self._nodes.remove(node)
            except KeyError:
                pass
            else:
                # Rebuild the wait set so it doesn't include this node
                if self._guard:
                    self._guard.trigger()

    def wake(self) -> None:
        """Wake the executor because something changed."""
        if self._guard:
            self._guard.trigger()

    def get_nodes(self) -> List["Node"]:
        """Return nodes that have been added to this executor."""
        with self._nodes_lock:
            return list(self._nodes)

    def shutdown(self) -> None:
        """Stop executing callbacks and clean up resources."""
        with self._shutdown_lock:
            if not self._is_shutdown:
                self._is_shutdown = True
                if self._guard:
                    self._guard.trigger()

        # Clean up nodes
        with self._nodes_lock:
            self._nodes = set()

        # Clean up guard conditions
        with self._shutdown_lock:
            if self._guard:
                self._guard.destroy()
                self._guard = None
            if self._sigint_gc:
                self._sigint_gc.destroy()
                self._sigint_gc = None

    def _spin_once(self) -> None:
        """
        Execute a single iteration of the wait loop.

        This method waits for callbacks to become ready and dispatches them
        to the task group for execution.
        """
        if self._is_shutdown or not self._context.ok():
            return

        # Get nodes to wait on
        with self._nodes_lock:
            nodes_to_use = list(self._nodes)

        # Gather entities from all nodes
        subscriptions: List[Subscription] = []
        guards: List[GuardCondition] = []
        timers: List[Timer] = []
        clients: List[Client] = []
        services: List[Service] = []
        waitables: List[Waitable] = []

        for node in nodes_to_use:
            subscriptions.extend(
                filter(lambda e: self._can_execute(e), node.subscriptions)
            )
            timers.extend(filter(lambda e: self._can_execute(e), node.timers))
            clients.extend(filter(lambda e: self._can_execute(e), node.clients))
            services.extend(filter(lambda e: self._can_execute(e), node.services))
            node_guards = filter(lambda e: self._can_execute(e), node.guards)
            waitables.extend(filter(lambda e: self._can_execute(e), node.waitables))
            # Retrigger guard conditions that were triggered but not handled
            for gc in node_guards:
                if gc._executor_triggered:
                    gc.trigger()
                guards.append(gc)

        if self._guard:
            guards.append(self._guard)
        if self._sigint_gc:
            guards.append(self._sigint_gc)

        entity_count = NumberOfEntities(
            len(subscriptions), len(guards), len(timers), len(clients), len(services)
        )

        # Build and wait on wait set
        with ExitStack() as context_stack:
            sub_handles = []
            for sub in subscriptions:
                try:
                    context_stack.enter_context(sub.handle)
                    sub_handles.append(sub.handle)
                except InvalidHandle:
                    entity_count.num_subscriptions -= 1

            client_handles = []
            for cli in clients:
                try:
                    context_stack.enter_context(cli.handle)
                    client_handles.append(cli.handle)
                except InvalidHandle:
                    entity_count.num_clients -= 1

            service_handles = []
            for srv in services:
                try:
                    context_stack.enter_context(srv.handle)
                    service_handles.append(srv.handle)
                except InvalidHandle:
                    entity_count.num_services -= 1

            timer_handles = []
            for tmr in timers:
                try:
                    context_stack.enter_context(tmr.handle)
                    timer_handles.append(tmr.handle)
                except InvalidHandle:
                    entity_count.num_timers -= 1

            guard_handles = []
            for gc in guards:
                try:
                    context_stack.enter_context(gc.handle)
                    guard_handles.append(gc.handle)
                except InvalidHandle:
                    entity_count.num_guard_conditions -= 1

            for waitable in waitables:
                try:
                    context_stack.enter_context(waitable)
                    entity_count += waitable.get_num_entities()
                except InvalidHandle:
                    pass

            if self._context.handle is None:
                raise RuntimeError("Cannot enter context if context is None")

            context_stack.enter_context(self._context.handle)

            wait_set = _rclpy.WaitSet(
                entity_count.num_subscriptions,
                entity_count.num_guard_conditions,
                entity_count.num_timers,
                entity_count.num_clients,
                entity_count.num_services,
                entity_count.num_events,
                self._context.handle,
            )

            wait_set.clear_entities()
            for sub_handle in sub_handles:
                wait_set.add_subscription(sub_handle)
            for cli_handle in client_handles:
                wait_set.add_client(cli_handle)
            for srv_capsule in service_handles:
                wait_set.add_service(srv_capsule)
            for tmr_handle in timer_handles:
                wait_set.add_timer(tmr_handle)
            for gc_handle in guard_handles:
                wait_set.add_guard_condition(gc_handle)
            for waitable in waitables:
                waitable.add_to_wait_set(wait_set)

            # Wait with configured timeout to allow shutdown checks
            wait_set.wait(timeout_sec_to_nsec(self._wait_timeout_sec))

            if self._is_shutdown or not self._context.ok():
                return

            # Get ready entities
            subs_ready = wait_set.get_ready_entities("subscription")
            guards_ready = wait_set.get_ready_entities("guard_condition")
            timers_ready = wait_set.get_ready_entities("timer")
            clients_ready = wait_set.get_ready_entities("client")
            services_ready = wait_set.get_ready_entities("service")

            # Mark all guards as triggered
            for gc in guards:
                if gc.handle.pointer in guards_ready:
                    gc._executor_triggered = True

            # Process ready entities
            for node in nodes_to_use:
                # Process timers
                for tmr in node.timers:
                    if tmr.handle.pointer in timers_ready:
                        if tmr.handle.is_timer_ready():
                            if self._can_execute(tmr):
                                self._execute_timer(tmr)

                # Process subscriptions
                for sub in node.subscriptions:
                    if sub.handle.pointer in subs_ready:
                        if self._can_execute(sub):
                            self._execute_subscription(sub)

                # Process guard conditions
                for gc in node.guards:
                    if gc._executor_triggered:
                        if self._can_execute(gc):
                            self._execute_guard_condition(gc)

                # Process clients
                for client in node.clients:
                    if client.handle.pointer in clients_ready:
                        if self._can_execute(client):
                            self._execute_client(client)

                # Process services
                for srv in node.services:
                    if srv.handle.pointer in services_ready:
                        if self._can_execute(srv):
                            self._execute_service(srv)

                # Process waitables
                for wt in node.waitables:
                    if wt in waitables and wt.is_ready(wait_set):
                        if self._can_execute(wt):
                            self._execute_waitable(wt)

    def _can_execute(self, entity: "Entity") -> bool:
        """Check if an entity's callback can be executed."""
        return (
            not hasattr(entity, "_executor_event") or not entity._executor_event
        ) and (
            entity.callback_group is None or entity.callback_group.can_execute(entity)
        )

    def _execute_timer(self, tmr: Timer) -> None:
        """Execute a timer callback in the task group."""
        try:
            with tmr.handle:
                tmr.handle.call_timer()
                if tmr.callback:

                    async def _execute() -> None:
                        try:
                            await await_or_execute(tmr.callback)
                        finally:
                            if tmr.callback_group:
                                tmr.callback_group.ending_execution(tmr)

                    if tmr.callback_group:
                        tmr.callback_group.beginning_execution(tmr)

                    self._execute_in_task_group(_execute)
        except InvalidHandle:
            pass

    def _execute_subscription(self, sub: Subscription) -> None:
        """Execute a subscription callback in the task group."""
        try:
            with sub.handle:
                msg_info = sub.handle.take_message(sub.msg_type, sub.raw)
                if msg_info is None:
                    return

                msg = msg_info[0]

                async def _execute(msg) -> None:
                    try:
                        await await_or_execute(sub.callback, msg)
                    finally:
                        if sub.callback_group:
                            sub.callback_group.ending_execution(sub)

                if sub.callback_group:
                    sub.callback_group.beginning_execution(sub)

                self._execute_in_task_group(_execute, msg)
        except InvalidHandle:
            pass

    def _execute_guard_condition(self, gc: GuardCondition) -> None:
        """Execute a guard condition callback in the task group."""
        gc._executor_triggered = False

        async def _execute() -> None:
            try:
                if gc.callback:
                    await await_or_execute(gc.callback)
            finally:
                if gc.callback_group:
                    gc.callback_group.ending_execution(gc)

        if gc.callback_group:
            gc.callback_group.beginning_execution(gc)

        self._execute_in_task_group(_execute)

    def _execute_client(self, client: Client) -> None:
        """Execute a client callback in the task group."""
        try:
            with client.handle:
                header_and_response = client.handle.take_response(
                    client.srv_type.Response  # type: ignore
                )

            async def _execute() -> None:
                try:
                    header, response = header_and_response
                    if header is None:
                        return
                    try:
                        sequence = header.request_id.sequence_number
                        future = client.get_pending_request(sequence)
                    except KeyError:
                        # The request was cancelled
                        pass
                    else:
                        future.set_result(response)
                finally:
                    if client.callback_group:
                        client.callback_group.ending_execution(client)

            if client.callback_group:
                client.callback_group.beginning_execution(client)

            self._execute_in_task_group(_execute)
        except InvalidHandle:
            pass

    def _execute_service(self, srv: Service) -> None:
        """Execute a service callback in the task group."""
        try:
            with srv.handle:
                request_and_header = srv.handle.service_take_request(
                    srv.srv_type.Request  # type: ignore
                )

            async def _execute() -> None:
                try:
                    request, header = request_and_header
                    if header is None:
                        return

                    response = await await_or_execute(
                        srv.callback,
                        request,
                        srv.srv_type.Response(),  # type: ignore
                    )
                    srv.send_response(response, header)
                finally:
                    if srv.callback_group:
                        srv.callback_group.ending_execution(srv)

            if srv.callback_group:
                srv.callback_group.beginning_execution(srv)

            self._execute_in_task_group(_execute)
        except InvalidHandle:
            pass

    def _execute_waitable(self, waitable: Waitable) -> None:
        """Execute a waitable callback in the task group."""
        data = waitable.take_data()

        async def _execute() -> None:
            try:
                await waitable.execute(data)
            finally:
                if waitable.callback_group:
                    waitable.callback_group.ending_execution(waitable)

        if waitable.callback_group:
            waitable.callback_group.beginning_execution(waitable)

        self._execute_in_task_group(_execute)

    def _spin_loop(self) -> None:
        """Main spin loop running in a background thread."""
        while not self._is_shutdown and self._context.ok():
            try:
                self._spin_once()
            except Exception:
                # Continue spinning even if there's an error
                pass

    def create_task(self, coro: Callable[..., Awaitable[None]], *args):
        """Create a task in the task group."""
        self._task_group.start_soon(coro, *args)

    def _execute_in_task_group(
        self, coro: Callable[..., Awaitable[None]], *args
    ) -> None:
        """Helper to execute a function in the task group."""
        self._portal.start_task_soon(self._task_group.start_soon, coro, *args)

    @asynccontextmanager
    async def __asynccontextmanager__(self):
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            async with anyio.from_thread.BlockingPortal() as portal:
                self._portal = portal
                self._spin_thread = threading.Thread(
                    target=self._spin_loop, daemon=True
                )
                self._spin_thread.start()
                try:
                    yield self
                finally:
                    self.shutdown()

                    # Wait for spin thread to finish
                    if self._spin_thread:
                        self._spin_thread.join(timeout=1.0)
