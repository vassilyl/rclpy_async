"""
ROS 2 Async Subscription Example - Next Message

This script demonstrates how to use rclpy_async to create an asynchronous ROS 2 node
that subscribes to turtle pose messages from the turtlesim package. The subscription
awaits the next available message from the '/turtle1/pose' topic and prints it.

Key features:
- Uses anyio for asynchronous programming
- Creates a memory object stream for message passing
- Subscribes to turtlesim pose messages
- Receives and prints a single message before exiting

Usage:
    Run `python examples/subscribe_next_update.py`

Note: Requires turtlesim to be running for messages to be available.
"""

import anyio
import rclpy
import turtlesim.msg

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
    node = rclpy.create_node("subscription_last_node")
    node.create_subscription(
        turtlesim.msg.Pose,
        "/turtle1/pose",
        send_no_wait_no_raise,
        qos_profile=0,  # does not queue messages in middleware queue
    )

    async with rclpy_async.AsyncExecutor() as xtor:
        xtor.add_node(node)
        # wait for the next message to arrive
        msg = await receive_stream.receive()
        print(msg)


anyio.run(main)
