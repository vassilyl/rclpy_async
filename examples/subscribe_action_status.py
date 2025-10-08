"""
ROS 2 Async Action Status Subscription Example

This script demonstrates how to use rclpy_async to create an asynchronous ROS 2 node
that subscribes to action status messages from the turtle rotate_absolute action.
The subscription receives and prints messages in a loop until the user presses Enter.

Key features:
- Uses anyio for asynchronous programming
- Creates a memory object stream for message passing
- Subscribes to action status messages with transient local QoS
- Continuously receives and prints messages until interrupted
- Graceful shutdown on keyboard input

Usage:
    Run `python examples/subscribe_action_status.py`
    Press Enter to stop the subscription loop.

Note: Requires turtlesim to be running. If no action goal has been sent,
no messages will be received. You can send a goal using the example:
`examples/action_call.py` or `examples/action_call_cancel.py`.
Alternatively, from command line:
    ros2 action send_goal /turtle1/rotate_absolute turtlesim/action/RotateAbsolute '{"theta": 5.0}'
"""

from datetime import datetime, timezone
import threading

import anyio
import anyio.from_thread
from action_msgs.msg import GoalStatusArray
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy

from rclpy_async import NodeAsync, goal_status_str, goal_uuid_str


def wait_for_enter(portal, cancel_scope):
    # to be executed on a daemon thread
    input("Press Enter to stop awaiting messages...\n")
    print("Cancelling message processing loop...")
    portal.call(cancel_scope.cancel)


def format_ros_time(stamp) -> str:
    """Convert ROS time (sec, nanosec) to an ISO 8601 string in UTC."""
    try:
        total_seconds = stamp.sec + stamp.nanosec / 1_000_000_000
        dt = datetime.fromtimestamp(total_seconds, tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError, OSError):  # OSError for out-of-range values
        return f"{stamp.sec}.{stamp.nanosec:09d}"


async def message_receiver(receive_stream):
    """Continuously receive goal status updates and print formatted output."""
    message_count = 0
    while True:
        msg = await receive_stream.receive()
        message_count += 1

        print(f"\nMessage #{message_count} with {len(msg.status_list)} status entries:")

        for index, status in enumerate(msg.status_list, start=1):
            goal_uuid = goal_uuid_str(status.goal_info.goal_id.uuid)
            status_label = goal_status_str(status.status)
            goal_stamp = format_ros_time(status.goal_info.stamp)

            print(f"  [{goal_stamp}] goal {goal_uuid} {status_label}")


async def main():
    CancelError = anyio.get_cancelled_exc_class()
    try:
        async with NodeAsync(
            "action_status_subscription_node"
        ) as anode:
            # Create a pair of memory streams, middleware to deal with buffering
            send_stream, receive_stream = anyio.create_memory_object_stream()

            # Create QoS profile with transient local durability for action status
            qos_profile = QoSProfile(
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                reliability=QoSReliabilityPolicy.RELIABLE,
                depth=10,
            )

            async with send_stream, receive_stream:
                topic = "/turtle1/rotate_absolute/_action/status"
                with anode.subscription(
                    GoalStatusArray,
                    topic,
                    # do not skip messages, block the subscription callback thread
                    # until message_receiver starts processing the message
                    send_stream.send,
                    qos_profile=qos_profile,
                ):
                    print(f"Listening for messages on {topic}")
                    # await message_receiver(receive_stream)

                    with anyio.CancelScope() as scope:
                        # start a daemon thread to cancel the scope on user request
                        async with anyio.from_thread.BlockingPortal() as portal:
                            threading.Thread(
                                target=wait_for_enter, args=(portal, scope), daemon=True
                            ).start()
                            # run the message receiver until the scope is cancelled
                            await message_receiver(receive_stream)
    except CancelError:
        print("Cancelled by a SIGINT event or Ctrl+C. Exiting...")


if __name__ == "__main__":
    anyio.run(main)
