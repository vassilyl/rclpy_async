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

Note: Requires turtlesim to be running and rotate_absolute actions to be available.
"""

import anyio
import anyio.from_thread
import rclpy_async
from action_msgs.msg import GoalStatusArray, GoalStatus
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy
import uuid
from datetime import datetime, timezone
import threading


STATUS_LABELS = {
    GoalStatus.STATUS_UNKNOWN: "UNKNOWN",
    GoalStatus.STATUS_ACCEPTED: "ACCEPTED",
    GoalStatus.STATUS_EXECUTING: "EXECUTING",
    GoalStatus.STATUS_CANCELING: "CANCELING",
    GoalStatus.STATUS_SUCCEEDED: "SUCCEEDED",
    GoalStatus.STATUS_CANCELED: "CANCELED",
    GoalStatus.STATUS_ABORTED: "ABORTED",
}


def wait_for_enter(portal, cancel_scope):
    # to be executed on a daemon thread
    input("Press Enter to stop awaiting messages...\n")
    portal.call(cancel_scope.cancel)
    print("Message processing loop cancelled.")


def format_goal_uuid(uuid_seq) -> str:
    """Convert a sequence of bytes into a canonical UUID string."""
    try:
        goal_uuid = uuid.UUID(bytes=bytes(uuid_seq))
        return str(goal_uuid)
    except (ValueError, TypeError):
        return "".join(f"{byte:02x}" for byte in uuid_seq)


def describe_status(code: int) -> str:
    return STATUS_LABELS.get(code, f"UNKNOWN({code})")


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
            goal_uuid = format_goal_uuid(status.goal_info.goal_id.uuid)
            status_label = describe_status(status.status)
            goal_stamp = format_ros_time(status.goal_info.stamp)

            print(f"  [{goal_stamp}] goal {goal_uuid} {status_label}")


async def main():
    CancelError = anyio.get_cancelled_exc_class()
    try:
        async with anyio.from_thread.BlockingPortal() as portal:
            async with rclpy_async.NodeAsync(
                portal, "action_status_subscription_node"
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
                    async with anode.create_subscription(
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
                            threading.Thread(
                                target=wait_for_enter, args=(portal, scope), daemon=True
                            ).start()
                            await message_receiver(receive_stream)
    except CancelError:
        print("Cancelled by a SIGINT event or Ctrl+C. Exiting...")


if __name__ == "__main__":
    anyio.run(main)
