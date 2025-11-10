"""Example of cancelling a turtlesim RotateAbsolute action with rclpy_async and anyio.

This script sends two RotateAbsolute goals and cancels the second when feedback reports
the remaining angle below one radian.

Run:
    python examples/action_call_cancel.py
    Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node"""

import anyio
import rclpy
from turtlesim.action import RotateAbsolute

import rclpy_async

action_name = "/turtle1/rotate_absolute"


async def main():
    rclpy.init()
    node = rclpy.create_node("action_cancel_node")
    logger = node.get_logger()

    async with rclpy_async.start_executor() as xtor:
        xtor.add_node(node)
        # First, rotate to 0 radians to have a known starting orientation
        with rclpy_async.action_client(
            node, RotateAbsolute, action_name
        ) as rotate_absolute:
            logger.info("Sending RotateAbsolute goal to 0.0 radians")
            await rotate_absolute(RotateAbsolute.Goal(theta=0.0))
            logger.info("Rotation to 0.0 radians complete")

            # Now rotate to 2 radians, but cancel when remaining angle < 1 radian
            cancel_scope = anyio.CancelScope()

            async def feedback_task(msg):
                logger.info(f"Feedback: remaining={msg.feedback.remaining}")
                if abs(msg.feedback.remaining) < 1.0:
                    logger.info("Feedback: cancelling the goal")
                    cancel_scope.cancel()

            with cancel_scope:
                logger.info("Sending RotateAbsolute goal to 2.0 radians")
                await rotate_absolute(
                    RotateAbsolute.Goal(theta=2.0), feedback_task=feedback_task
                )
                logger.error("This message should never be logged")
            logger.info("Rotation to 2.0 radians cancelled")


anyio.run(main)
