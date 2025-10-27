import logging

import anyio
from example_interfaces.action import Fibonacci

import rclpy_async

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def execute_callback(goal_handle):
    logger.info("Executing goal...")

    feedback_msg = Fibonacci.Feedback()
    feedback_msg.sequence = [0, 1]

    for i in range(1, goal_handle.request.order):
        feedback_msg.sequence.append(
            feedback_msg.sequence[i] + feedback_msg.sequence[i - 1]
        )
        logger.info("Feedback: {0}".format(feedback_msg.sequence))
        goal_handle.publish_feedback(feedback_msg)
        await anyio.sleep(1)

    goal_handle.succeed()

    result = Fibonacci.Result()
    result.sequence = feedback_msg.sequence
    return result


async def main():
    CancelError = anyio.get_cancelled_exc_class()
    try:
        async with rclpy_async.NodeAsync("fibonacci_node") as anode:
            with anode.action_server(Fibonacci, "fibonacci", execute_callback):
                print(
                    "Action '/fibonacci' type 'example_interfaces/action/Fibonacci' is ready."
                )
                print(
                    "You can call it with "
                    "'ros2 action send_goal /fibonacci example_interfaces/action/Fibonacci \"order: 5\"'"
                )
                await anyio.sleep_forever()
    except CancelError:
        print("Ctrl+C detected, shutting down service...")


anyio.run(main)
