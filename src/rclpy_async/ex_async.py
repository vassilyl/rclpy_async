from contextlib import asynccontextmanager
from contextlib import ExitStack
from inspect import iscoroutinefunction

import rclpy
import rclpy.executors
from rclpy.timer import Timer
from rclpy.subscription import Subscription
from rclpy.guard_condition import GuardCondition
from rclpy.service import Service
from rclpy.client import Client
from rclpy.waitable import Waitable
from rclpy.utilities import timeout_sec_to_nsec
from rclpy.exceptions import InvalidHandle

import anyio
import anyio.from_thread

class AnyioExecutor(rclpy.executors.Executor, anyio.AsyncContextManagerMixin):
    """An rclpy Executor that integrates with AnyIO event loop."""

    def __init__(self, *, context: rclpy.Context) -> None:
        super().__init__(context=context)

    def __spin(self) -> None:
        """Spin the executor once."""
        try:
            while True:
                for handler, entity, node in self._wait_for_ready_callbacks(timeout_sec=0.1):
                    if iscoroutinefunction(handler):
                        anyio.from_thread.run(self._task_group.start_soon, handler, entity, node)
                    else:
                        anyio.from_thread.run_sync(handler, entity, node)

        except rclpy.executors.ExternalShutdownException:
            pass

    @asynccontextmanager
    async def run(self):
        """Async context manager to run the executor."""
        try:
            self._task_group = anyio.create_task_group()
            yield self
        finally:
            self.shutdown()

    def create_task(self, callback: rclpy.executors.Coroutine, *args, **kwargs) -> rclpy.executors.Task:
        # create a task and schedule its execution
        raise NotImplementedError("AnyIOExecutor.create_task is not implemented yet.")

    def _make_handler(
        self,
        entity: WaitableEntityType,
        node: 'Node',
        take_from_wait_list: Callable,
        call_coroutine: Coroutine
    ) -> Task:
        """
        Make a handler that performs work on an entity.

        :param entity: An entity to wait on.
        :param node: The node associated with the entity.
        :param take_from_wait_list: Makes the entity to stop appearing in the wait list.
        :param call_coroutine: Does the work the entity is ready for
        """
        # Mark this so it doesn't get added back to the wait list
        entity._executor_event = True

        async def handler(entity, gc, is_shutdown, work_tracker):
            if is_shutdown or not entity.callback_group.beginning_execution(entity):
                # Didn't get the callback, or the executor has been ordered to stop
                entity._executor_event = False
                gc.trigger()
                return
            with work_tracker:
                arg = take_from_wait_list(entity)

                # Signal that this has been 'taken' and can be added back to the wait list
                entity._executor_event = False
                gc.trigger()

                try:
                    await call_coroutine(entity, arg)
                finally:
                    entity.callback_group.ending_execution(entity)
                    # Signal that work has been done so the next callback in a mutually exclusive
                    # callback group can get executed
                    gc.trigger()
        return task

    def _wait_for_ros_events(
        self,
        timeout_sec: float | None = None,
        nodes: List['Node'] = None,
        condition: Callable[[], bool] = lambda: False,
    ) -> Generator[Tuple[Task, WaitableEntityType, 'Node'], None, None]:
        """
        Yield callbacks that are ready to be executed.

        :raise TimeoutException: on timeout.
        :raise ShutdownException: on if executor was shut down.

        :param timeout_sec: Seconds to wait. Block forever if ``None`` or negative.
            Don't wait if 0.
        :param nodes: A list of nodes to wait on. Wait on all nodes if ``None``.
        :param condition: A callable that makes the function return immediately when it evaluates
            to True.
        """
        timeout_timer = None
        timeout_nsec = timeout_sec_to_nsec(timeout_sec)
        if timeout_nsec > 0:
            timeout_timer = Timer(None, None, timeout_nsec, self._clock, context=self._context)

        yielded_work = False
        while not yielded_work and not self._is_shutdown and not condition():
            # Refresh "all" nodes in case executor was woken by a node being added or removed
            nodes_to_use = nodes
            if nodes is None:
                nodes_to_use = self.get_nodes()

            # Gather entities that can be waited on
            subscriptions: list[Subscription] = []
            guards: list[GuardCondition] = []
            timers: list[Timer] = []
            clients: list[Client] = []
            services: list[Service] = []
            waitables: list[Waitable] = []
            for node in nodes_to_use:
                subscriptions.extend(filter(self.can_execute, node.subscriptions))
                timers.extend(filter(self.can_execute, node.timers))
                clients.extend(filter(self.can_execute, node.clients))
                services.extend(filter(self.can_execute, node.services))
                node_guards = filter(self.can_execute, node.guards)
                waitables.extend(filter(self.can_execute, node.waitables))
                # retrigger a guard condition that was triggered but not handled
                for gc in node_guards:
                    if gc._executor_triggered:
                        gc.trigger()
                    guards.append(gc)
            if timeout_timer is not None:
                timers.append(timeout_timer)

            guards.append(self._guard)
            guards.append(self._sigint_gc)

            entity_count = NumberOfEntities(
                len(subscriptions), len(guards), len(timers), len(clients), len(services))

            # Construct, wait, extract and destroy a wait set
            wait_set = None
            with ExitStack() as context_stack:
                # Build list of handles to put in wait set
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

                context_stack.enter_context(self._context.handle)

                # Allocate and fill in wait set with the handles

                wait_set = _rclpy.WaitSet(
                    entity_count.num_subscriptions,
                    entity_count.num_guard_conditions,
                    entity_count.num_timers,
                    entity_count.num_clients,
                    entity_count.num_services,
                    entity_count.num_events,
                    self._context.handle)

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

                # Wait for something to become ready
                wait_set.wait(timeout_nsec)
                if self._is_shutdown:
                    raise ShutdownException()
                if not self._context.ok():
                    raise ExternalShutdownException()

                # get ready entities
                subs_ready = wait_set.get_ready_entities('subscription')
                guards_ready = wait_set.get_ready_entities('guard_condition')
                timers_ready = wait_set.get_ready_entities('timer')
                clients_ready = wait_set.get_ready_entities('client')
                services_ready = wait_set.get_ready_entities('service')

                # Mark all guards as triggered before yielding since they're auto-taken
                for gc in guards:
                    if gc.handle.pointer in guards_ready:
                        gc._executor_triggered = True

                # Check waitables before wait set is destroyed
                for node in nodes_to_use:
                    for wt in node.waitables:
                        # Only check waitables that were added to the wait set
                        if wt in waitables and wt.is_ready(wait_set):
                            if wt.callback_group.can_execute(wt):
                                handler = self._make_handler(
                                    wt, node, lambda e: e.take_data(), self._execute_waitable)
                                yielded_work = True
                                yield handler, wt, node

            # Process ready entities one node at a time
            for node in nodes_to_use:
                for tmr in node.timers:
                    if tmr.handle.pointer in timers_ready:
                        # Check timer is ready to workaround rcl issue with cancelled timers
                        if tmr.handle.is_timer_ready():
                            if tmr.callback_group.can_execute(tmr):
                                handler = self._make_handler(
                                    tmr, node, self._take_timer, self._execute_timer)
                                yielded_work = True
                                yield handler, tmr, node

                for sub in node.subscriptions:
                    if sub.handle.pointer in subs_ready:
                        if sub.callback_group.can_execute(sub):
                            handler = self._make_handler(
                                sub, node, self._take_subscription, self._execute_subscription)
                            yielded_work = True
                            yield handler, sub, node

                for gc in node.guards:
                    if gc._executor_triggered:
                        if gc.callback_group.can_execute(gc):
                            handler = self._make_handler(
                                gc, node, self._take_guard_condition,
                                self._execute_guard_condition)
                            yielded_work = True
                            yield handler, gc, node

                for client in node.clients:
                    if client.handle.pointer in clients_ready:
                        if client.callback_group.can_execute(client):
                            handler = self._make_handler(
                                client, node, self._take_client, self._execute_client)
                            yielded_work = True
                            yield handler, client, node

                for srv in node.services:
                    if srv.handle.pointer in services_ready:
                        if srv.callback_group.can_execute(srv):
                            handler = self._make_handler(
                                srv, node, self._take_service, self._execute_service)
                            yielded_work = True
                            yield handler, srv, node

            # Check timeout timer
            if (
                timeout_nsec == 0 or
                (timeout_timer is not None and timeout_timer.handle.pointer in timers_ready)
            ):
                raise TimeoutException()
        if self._is_shutdown:
            raise ShutdownException()
        if condition():
            raise ConditionReachedException()

    def wait_for_ready_callbacks(self, *args, **kwargs) -> Tuple[Task, WaitableEntityType, 'Node']:
        raise NotImplementedError("AnyIOExecutor.wait_for_ready_callbacks is not implemented yet.")