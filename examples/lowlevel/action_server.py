import anyio
from example_interfaces.action import Fibonacci

import rclpy
import rclpy.action
import rclpy_async

help = """
Action '/fibonacci' type 'example_interfaces/action/Fibonacci' is ready.
You can call it with
                  
    ros2 action send_goal /fibonacci example_interfaces/action/Fibonacci "order: 5"
"""

async def main():
    rclpy.init()
    node = rclpy.create_node("fibonacci_action_server_node")
    logger = node.get_logger()

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

    rclpy.action.ActionServer(node, Fibonacci, "fibonacci", execute_callback)

    try:
        async with rclpy_async.start_xtor() as xctor:
            xctor.add_node(node)
            print(help)
            await anyio.sleep_forever()
    except anyio.get_cancelled_exc_class():
        print("Ctrl+C detected, shutting down service...")


anyio.run(main)
