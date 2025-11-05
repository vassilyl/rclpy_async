"""Example of calling a service and subscribing to a topic using rclpy_async and anyio.

This script demonstrates how to create an asynchronous ROS 2 node that subscribes to
turtle pose messages from the turtlesim package, calls a service to teleport the turtle,
and prints the pose before and after the teleportation.

Run:
    python examples/call_service.py
    Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node"""

import anyio
import rclpy
import turtlesim.msg
import turtlesim.srv

import rclpy_async

# Zero buffer stream doesn't keep messages.
# If a receiver awaits a message the stream passes sent message to the receiver.
# If no receiver is waiting the message the sender is blocked or the message is dropped.
send_stream, receive_stream = anyio.create_memory_object_stream(0)


# Subscription callback sends topic messages to an awaiting receiver,
# or drops them if no receiver is waiting.
def send_no_wait_no_raise(msg):
    """Send message without waiting receiver and without raising WouldBlock."""
    try:
        send_stream.send_nowait(msg)
    except anyio.WouldBlock:
        pass


async def main():
    rclpy.init()
    node = rclpy.create_node("service_call_node")
    node.create_subscription(
        turtlesim.msg.Pose,
        "/turtle1/pose",
        send_no_wait_no_raise,
        qos_profile=0,  # does not queue messages in middleware queue
    )
    request = turtlesim.srv.TeleportRelative.Request()
    request.linear = 2.0
    request.angular = 1.57

    async with rclpy_async.start_xtor() as xtor:
        xtor.add_node(node)
        with rclpy_async.service_client(
            node,
            turtlesim.srv.TeleportRelative,
            "/turtle1/teleport_relative",
        ) as teleport_relative:
            before = await receive_stream.receive()
            print(f"Pose before: {before}")

            print(
                f"Teleport relative linear={request.linear}, angular={request.angular}"
            )
            resp = await teleport_relative(request)
            print(f"Response: {resp}")

            after = await receive_stream.receive()
            print(f"Pose after: {after}")


anyio.run(main)
