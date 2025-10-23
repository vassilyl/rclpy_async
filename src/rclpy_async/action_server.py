from anyio import CancelScope
import rclpy.action.server

from .executor import DualThreadExecutor


class ActionServerAsync(rclpy.action.ActionServer):
    """Asynchronous Action Server using rclpy_async and anyio."""

    def __init__(
        self,
        node,
        action_type,
        action_name,
        execute_callback,
        *,
        goal_callback=rclpy.action.server.default_goal_callback,
        handle_accepted_callback=rclpy.action.server.default_handle_accepted_callback,
        accept_cancellations=True,
        goal_service_qos_profile=rclpy.action.server.qos_profile_services_default,
        result_service_qos_profile=rclpy.action.server.qos_profile_services_default,
        cancel_service_qos_profile=rclpy.action.server.qos_profile_services_default,
        feedback_pub_qos_profile=rclpy.action.server.QoSProfile(depth=10),
        status_pub_qos_profile=rclpy.action.server.qos_profile_action_status_default,
        result_timeout=900,
    ):
        super().__init__(
            node,
            action_type,
            action_name,
            execute_callback,
            goal_callback=goal_callback,
            handle_accepted_callback=handle_accepted_callback,
            cancel_callback=self._accept_cancellation
            if accept_cancellations
            else rclpy.action.server.default_cancel_callback,
            goal_service_qos_profile=goal_service_qos_profile,
            result_service_qos_profile=result_service_qos_profile,
            cancel_service_qos_profile=cancel_service_qos_profile,
            feedback_pub_qos_profile=feedback_pub_qos_profile,
            status_pub_qos_profile=status_pub_qos_profile,
            result_timeout=result_timeout,
        )
        self._cancellation_scopes = {}

    @property
    def executor(self) -> DualThreadExecutor:
        executor = self._node.executor
        assert isinstance(executor, DualThreadExecutor), (
            "Node's executor must be DualThreadExecutor"
        )
        return executor

    def _accept_cancellation(self, goal_handle):
        goal_id = bytes(goal_handle.goal_id.uuid)
        cancel_scope = self._cancellation_scopes.get(goal_id, None)
        if isinstance(cancel_scope, CancelScope):
            task = self.executor.to_task(cancel_scope.cancel)
            if task is not None:
                task()
                return rclpy.action.server.CancelResponse.ACCEPT
        return rclpy.action.server.CancelResponse.REJECT

    async def _execute_cancellable_goal_request(self, request_header_and_message):
        cancel_scope = CancelScope()
        goal_id = bytes(request_header_and_message[1].goal_id.uuid)
        self._cancellation_scopes[goal_id] = cancel_scope
        with cancel_scope:
            await super()._execute_goal_request(request_header_and_message)
        goal_id = bytes(request_header_and_message[1].goal_id.uuid)
        del self._cancellation_scopes[goal_id]

    async def _execute_goal_request(self, request_header_and_message):
        execute_goal_task = self.executor.to_task(
            self._execute_cancellable_goal_request
        )
        if execute_goal_task is not None:
            execute_goal_task(request_header_and_message)
