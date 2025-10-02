#!/usr/bin/env python
"""Example usage of NodeAsync with turtlesim.

Run:
    python examples/turtlesim_sample.py

Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node
"""

import logging
import anyio
import anyio.from_thread
import anyio.streams.memory

from turtlesim.msg import Pose
from turtlesim.srv import TeleportRelative
from turtlesim.action import RotateAbsolute

from rclpy_async.nodeasync import NodeAsync

logger = logging.getLogger(__name__)


def clear_receive_buffer(
    receive_stream: anyio.streams.memory.MemoryObjectReceiveStream,
):
    try:
        while True:
            receive_stream.receive_nowait()
    except anyio.WouldBlock:
        return


async def main_async():
    async with anyio.from_thread.BlockingPortal() as portal:
        async with NodeAsync(portal, "anyio_turtlesim") as anode:
            send_stream, receive_stream = anyio.create_memory_object_stream()

            with anode.subscription(
                Pose, "/turtle1/pose", send_stream.send_nowait, qos_profile=1
            ):
                for _ in range(2):
                    pose = await receive_stream.receive()
                    logger.info(
                        f"[pose] x={pose.x:.3f} y={pose.y:.3f} theta={pose.theta:.3f} "
                        f"linear_velocity={pose.linear_velocity:.3f} angular_velocity={pose.angular_velocity:.3f}"
                    )
                async with anode.service_client(
                    TeleportRelative, "/turtle1/teleport_relative"
                ) as teleport_relative:
                    req = TeleportRelative.Request()
                    req.linear = 2.0
                    req.angular = 1.57
                    resp = await teleport_relative(req)
                    logger.info(f"TeleportRelative response: {resp}")
                clear_receive_buffer(receive_stream)
                pose = await receive_stream.receive()
                logger.info(
                    f"[pose] x={pose.x:.3f} y={pose.y:.3f} theta={pose.theta:.3f} "
                    f"linear_velocity={pose.linear_velocity:.3f} angular_velocity={pose.angular_velocity:.3f}"
                )

                async def feedback_handler(msg):
                    logger.info(f"RotateAbsolute feedback: {msg.feedback}")

                rotate_absolute_result = await anode.call_action(
                    action_type=RotateAbsolute,
                    action_name="/turtle1/rotate_absolute",
                    goal_msg=RotateAbsolute.Goal(theta=3.14),
                    feedback_handler_async=feedback_handler,
                )
                logger.info(
                    f"RotateAbsolute action completed with status {rotate_absolute_result}"
                )


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("rclpy_async").setLevel(logging.DEBUG)
    anyio.run(main_async)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        logger.info(f"Exiting due to {type(e).__name__}")
