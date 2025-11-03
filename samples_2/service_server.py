import anyio
from example_interfaces.srv import AddTwoInts

import rclpy
import rclpy_async


async def handle_add_two_ints(request, response):
    await anyio.sleep(1)  # simulate some work being done
    response.sum = request.a + request.b
    return response


async def main():
    rclpy.init()
    node = rclpy.create_node("add_two_ints_service_node")
    node.create_service(
        AddTwoInts,
        "add_two_ints",
        handle_add_two_ints,
    )
    async with rclpy_async.AsyncExecutor() as executor:
        executor.add_node(node)
        print ("""
Service 'add_two_ints' of type 'example_interfaces/srv/AddTwoInts' is ready.
You can call it with

    ros2 service call /add_two_ints example_interfaces/srv/AddTwoInts '{"a": 2, "b": 3}'
"""
        )
        await anyio.sleep_forever()


anyio.run(main)
