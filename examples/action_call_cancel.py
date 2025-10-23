"""Example of cancelling a turtlesim RotateAbsolute action with rclpy_async and anyio.

This script sends two RotateAbsolute goals and cancels the second when feedback reports
the remaining angle below one radian.

Run:
    python examples/action_call_cancel.py
    Requires that `turtlesim` is running, e.g.:
    ros2 run turtlesim turtlesim_node"""

import logging

import anyio
from turtlesim.action import RotateAbsolute

from rclpy_async import NodeAsync

action_name = "/turtle1/rotate_absolute"


async def main():
    async with NodeAsync("action_cancel_node") as anode:
        # First, rotate to 0 radians to have a known starting orientation
        with anode.action_client(RotateAbsolute, action_name) as rotate_absolute:
            logging.info("Sending RotateAbsolute goal to 0.0 radians")
            await rotate_absolute(RotateAbsolute.Goal(theta=0.0))
            logging.info("Rotation to 0.0 radians complete")

            # Now rotate to 2 radians, but cancel when remaining angle < 1 radian
            cancel_scope = anyio.CancelScope()

            async def feedback_task(msg):
                logging.info(f"Feedback: remaining={msg.feedback.remaining}")
                if abs(msg.feedback.remaining) < 1.0:
                    logging.info("Feedback: cancelling the goal")
                    cancel_scope.cancel()

            with cancel_scope:
                logging.info("Sending RotateAbsolute goal to 2.0 radians")
                await rotate_absolute(
                    RotateAbsolute.Goal(theta=2.0), feedback_task=feedback_task
                )
                logging.error("This message should never be logged")
            logging.info("Rotation to 2.0 radians cancelled")


logging.basicConfig(level=logging.INFO)
logging.getLogger("rclpy_async").setLevel(logging.WARNING)
anyio.run(main)
