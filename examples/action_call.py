"""Example of calling an action and subscribing to a topic using rclpy_async and anyio.

This script demonstrates how to create an asynchronous ROS 2 node that subscribes to
turtle pose messages from the turtlesim package, calls the RotateAbsolute action to rotate the turtle,
and prints the pose before and after the rotation.

Run:
    python examples_2/action_call.py
    Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node"""

import anyio
import rclpy
import turtlesim.msg
from turtlesim.action import RotateAbsolute

import rclpy_async


send_stream, receive_stream = anyio.create_memory_object_stream(0)


def send_no_wait_no_raise(msg):
    """Send message without waiting receiver and without raising WouldBlock."""
    try:
        send_stream.send_nowait(msg)
    except anyio.WouldBlock:
        pass


async def main():
    rclpy.init()
    node = rclpy.create_node("action_call_node")
    logger = node.get_logger()

    # Create subscription
    node.create_subscription(
        turtlesim.msg.Pose,
        "/turtle1/pose",
        send_no_wait_no_raise,
        qos_profile=0,  # does not queue messages in middleware queue
    )

    async with rclpy_async.start_executor() as xtor:
        xtor.add_node(node)
        with rclpy_async.action_client(
            node, RotateAbsolute, "/turtle1/rotate_absolute"
        ) as rotate_absolute:
            # Get pose before rotation
            before = await receive_stream.receive()
            logger.info(f"Pose before: {before}")

            result = await rotate_absolute(
                RotateAbsolute.Goal(theta=2.0),
                lambda msg: logger.info(f"Rotation feedback: {msg.feedback}"),  # type: ignore
            )
            logger.info(f"Rotation result: {result}")

            # Get pose after rotation
            after = await receive_stream.receive()
            logger.info(f"Pose after: {after}")


anyio.run(main)
