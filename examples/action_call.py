"""Example of calling an action and subscribing to a topic using rclpy_async and anyio.

This script demonstrates how to create an asynchronous ROS 2 node that subscribes to
turtle pose messages from the turtlesim package, calls the RotateAbsolute action to rotate the turtle,
and prints the pose before and after the rotation.

Run:
    python examples/action_call.py
    Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node"""

import anyio
import turtlesim.msg
from turtlesim.action import RotateAbsolute

from rclpy_async import NodeAsync


async def main():
    async with NodeAsync("subscription_last_node") as anode:
        # create a pair of memory streams without a buffer (size=0)
        send_stream, receive_stream = anyio.create_memory_object_stream(0)

        def send_no_wait_no_raise(msg):
            """Send message without waiting receiver and without raising WouldBlock."""
            try:
                send_stream.send_nowait(msg)
            except anyio.WouldBlock:
                pass

        with anode.subscription(
            turtlesim.msg.Pose,
            "/turtle1/pose",
            send_no_wait_no_raise,  # skip the message if noone is waiting
            qos_profile=0,  # does not queue messages in middleware queue
        ):
            with anode.action_client(
                RotateAbsolute, "/turtle1/rotate_absolute"
            ) as rotate_absolute:
                theta = 2.0
                before = await receive_stream.receive()
                print(f"Pose before: {before}")
                print(f"Rotate absolute angular={theta}")
                resp = await rotate_absolute(RotateAbsolute.Goal(theta=theta))
                print(f"Response: {resp}")
                after = await receive_stream.receive()
                print(f"Pose after: {after}")


anyio.run(main)
