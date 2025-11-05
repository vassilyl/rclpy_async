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
from rclpy.action import ActionClient

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
    node = rclpy.create_node("action_call_node")

    # Create subscription
    node.create_subscription(
        turtlesim.msg.Pose,
        "/turtle1/pose",
        send_no_wait_no_raise,
        qos_profile=0,  # does not queue messages in middleware queue
    )

    # Create action client
    action_client = ActionClient(node, RotateAbsolute, "/turtle1/rotate_absolute")

    async with rclpy_async.start_executor() as xtor:
        xtor.add_node(node)

        # Wait for action server to be available
        if not await rclpy_async.server_ready(action_client.server_is_ready):
            print("Action server '/turtle1/rotate_absolute' not available")
            return

        # Get pose before rotation
        before = await receive_stream.receive()
        print(f"Pose before: {before}")

        # Send goal
        goal_msg = RotateAbsolute.Goal(theta=2.0)
        print(f"Rotate absolute angular={goal_msg.theta}")

        goal = await rclpy_async.future_result(action_client.send_goal_async(goal_msg))

        if goal is None or not goal.accepted:
            print("Goal rejected by action server")
            return

        # Wait for result
        result = await rclpy_async.future_result(goal.get_result_async())

        if result is not None:
            print(f"Response: {result.result}")

        # Get pose after rotation
        after = await receive_stream.receive()
        print(f"Pose after: {after}")


anyio.run(main)
