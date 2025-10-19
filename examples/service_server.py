import anyio
from std_srvs.srv import SetBool

import rclpy_async


async def handle_set_bool(request, response):
    response.success = True
    response.message = f"Received request with data: {request.data}"
    return response


async def main():
    async with rclpy_async.NodeAsync("set_bool_service_node") as anode:
        with anode.service_server(SetBool, "set_bool", handle_set_bool):
            print("Service 'set_bool_service' is ready.")
            await anyio.sleep_forever()


anyio.run(main)
