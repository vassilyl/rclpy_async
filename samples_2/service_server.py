import anyio
import rclpy
from example_interfaces.srv import AddTwoInts

import rclpy_async

help = """
Service 'add_two_ints' of type 'example_interfaces/srv/AddTwoInts' is ready.
You can call it with

    ros2 service call /add_two_ints example_interfaces/srv/AddTwoInts '{"a": 2, "b": 3}'
"""

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
    try:
        async with rclpy_async.AsyncExecutor() as xtor:
            xtor.add_node(node)
            print (help)
            await anyio.sleep_forever()
    except anyio.get_cancelled_exc_class():
        print("Ctrl+C detected, shutting down service...")


anyio.run(main)
