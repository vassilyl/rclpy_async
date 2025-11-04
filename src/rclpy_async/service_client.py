from contextlib import contextmanager

import anyio

from rclpy.node import Node
from rclpy.qos import qos_profile_services_default
from rclpy_async.utilities import server_ready, future_result


@contextmanager
def service_client(
    node: Node,
    srv_type,
    srv_name: str,
    *,
    qos_profile=qos_profile_services_default,
    server_wait_timeout: float = 5.0,
):
    """Context manager yielding an async callable for a ROS 2 service client.

    The yielded coroutine sends ``request`` to the service server and awaits the
    response within the active AnyIO event loop. The service is created on entry
    and destroyed on exit.

    Parameters
    ----------
    node : Node
        Node used to create the client. The node must already be attached to an
        executor able to service its callbacks.
    srv_type : type
        ROS service type (for example ``std_srvs.srv.SetBool``).
    srv_name : str
        Fully qualified service name.
    qos_profile : QoSProfile, optional
        QoS profile for the client, defaults to ``qos_profile_services_default``.
    server_wait_timeout : float, optional
        Seconds to wait for the server to become available before raising
        ``TimeoutError``.
    """

    client = node.create_client(srv_type, srv_name, qos_profile=qos_profile)

    async def _call(request):
        if not await server_ready(client.service_is_ready, server_wait_timeout):
            raise TimeoutError(
                f"Service '{srv_name}' not available within {server_wait_timeout}s"
            )
        with anyio.fail_after(server_wait_timeout):
            return await future_result(client.call_async(request))

    try:
        yield _call
    finally:
        node.destroy_client(client)
