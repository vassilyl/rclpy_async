import anyio
import rclpy
import rclpy.callback_groups
from example_interfaces.srv import AddTwoInts

import rclpy_async


async def start_inner_service(*, task_status=anyio.TASK_STATUS_IGNORED):
    async with rclpy_async.AsyncExecutor() as xtor:
        node = rclpy.create_node("add_two_ints_service_node")
        logger = node.get_logger()

        async def handle_add_two_ints(request, response):
            logger.info(f"Received request: {request.a}, {request.b}")
            response.sum = request.a + request.b
            logger.info(f"Sending response: {response.sum}")
            return response

        node.create_service(AddTwoInts, "add_two_ints", handle_add_two_ints)
        xtor.add_node(node)
        task_status.started()
        await anyio.sleep_forever()


async def start_outer_service(*, task_status=anyio.TASK_STATUS_IGNORED):
    async with rclpy_async.AsyncExecutor() as xtor:
        reentrant = rclpy.callback_groups.ReentrantCallbackGroup()
        node = rclpy.create_node("add_twice_service_node")
        logger = node.get_logger()
        client_outer_server = node.create_client(
            AddTwoInts, "add_two_ints", callback_group=reentrant
        )

        async def call_inner_service(a, b):
            logger.info(f"Calling inner service with: {a}, {b}")
            future = client_outer_server.call_async(AddTwoInts.Request(a=a, b=b))
            result = await rclpy_async.future_result(future)
            logger.info(f"Inner service response: {result.sum}")
            return result

        async def handle_add_twice(request, response):
            logger.info(f"Received request: {request.a}, {request.b}")
            first_resp = await call_inner_service(request.a, request.b)
            second_resp = await call_inner_service(first_resp.sum, request.b)
            logger.info(f"Sending response: {second_resp.sum}")
            response.sum = second_resp.sum
            return response

        node.create_service(
            AddTwoInts, "add_twice", handle_add_twice, callback_group=reentrant
        )
        xtor.add_node(node)
        task_status.started()
        await anyio.sleep_forever()


async def main():
    rclpy.init()

    async with anyio.create_task_group() as tg:
        await tg.start(start_inner_service)
        await tg.start(start_outer_service)

        async with rclpy_async.AsyncExecutor() as xtor:
            node_client = rclpy.create_node("add_twice_client")
            client_outer = node_client.create_client(AddTwoInts, "add_twice")
            xtor.add_node(node_client)
            node_client.get_logger().info("Client performing request add_twice(2, 3)")

            request = AddTwoInts.Request(a=2, b=3)
            future = client_outer.call_async(request)
            result = await rclpy_async.future_result(future)
            node_client.get_logger().info(f"Client received response: {result.sum}")

        # stop the servers gracefully
        tg.cancel_scope.cancel()


anyio.run(main)
