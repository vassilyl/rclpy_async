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
import anyio.from_thread
import rclpy_async
import turtlesim.msg


async def main():
    async with anyio.from_thread.BlockingPortal() as portal:
        async with rclpy_async.NodeAsync(portal, "subscription_last_node") as anode:
            # create a pair of memory streams without a buffer (size=0)
            send_stream, receive_stream = anyio.create_memory_object_stream(0)
            async with anode.create_subscription(
                turtlesim.msg.Pose,
                "/turtle1/pose",
                send_stream.send_nowait,  # skip the message if noone is waiting
                qos_profile=0,  # does not queue messages in middleware queue
            ):
                # wait for the next message to arrive
                msg = await receive_stream.receive()
                print(msg)


anyio.run(main)
