import anyio
from std_srvs.srv import SetBool

from rclpy_async import NodeAsync


async def main():
    async with (
        NodeAsync("client") as client_node,
        NodeAsync("outer") as outer_node,
        NodeAsync("inner") as inner_node,
    ):
        with (
            client_node.service_client(SetBool, "/outer") as outer_client,
            outer_node.service_client(SetBool, "/inner") as inner_client,
        ):

            async def outer_server(request, response):
                outer_node.get_logger().info(
                    f"Outer server received request: {request.data}"
                )
                resp = await inner_client(SetBool.Request(data=request.data))
                assert isinstance(resp, SetBool.Response)
                outer_node.get_logger().info(
                    f"Outer server received inner response: {resp.message}"
                )
                response.success = True
                response.message = "Outer completed successfully"
                return response

            async def inner_server(request, response):
                inner_node.get_logger().info(
                    f"Inner server received request: {request.data}"
                )
                response.success = True
                response.message = "Inner completed successfully"
                return response

            with (
                inner_node.service_server(SetBool, "/inner", inner_server),
                outer_node.service_server(SetBool, "/outer", outer_server),
            ):
                client_node.get_logger().info("Calling outer service...")
                resp = await outer_client(SetBool.Request(data=True))
                assert isinstance(resp, SetBool.Response)
                client_node.get_logger().info(f"Outer service response: {resp.message}")


anyio.run(main)
