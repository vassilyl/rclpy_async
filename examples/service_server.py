import anyio
from std_srvs.srv import SetBool

import rclpy_async


async def handle_set_bool(request, response):
    print(f"Service received request with data: {request.data}")
    response.success = True
    response.message = "Completed successfully"
    return response


async def main():
    CancelError = anyio.get_cancelled_exc_class()
    try:
        async with rclpy_async.NodeAsync("set_bool_service_node") as anode:
            with anode.service_server(SetBool, "set_bool", handle_set_bool):
                print("Service '/set_bool' type 'std_srvs/srv/SetBool' is ready.")
                print(
                    "You can call it with "
                    "'ros2 service call /set_bool std_srvs/srv/SetBool \"data: true\"'"
                )
                await anyio.sleep_forever()
    except CancelError:
        print("Ctrl+C detected, shutting down service...")


anyio.run(main)
