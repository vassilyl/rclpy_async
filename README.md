# rclpy_async

rclpy_async lets regular ROS 2 `rclpy` nodes cooperate with [AnyIO](https://anyio.readthedocs.io/en/stable/index.html) style structured concurrency. It spins the ROS wait-set in a background thread, forwards callbacks into an AnyIO task group, and offers helpers for building async service and action workflows without hand-written thread bridges.

## What’s Included

- `start_executor()` — async context manager that wires a ROS-compatible wait loop into an AnyIO task group without relying on an `rclpy.executors.Executor`. Add your existing nodes and keep using their callbacks.
- `service_client()` — context manager that yields an awaitable service call wrapper, complete with availability checks and timeout handling.
- `action_client()` — helper that turns goal submission into a single awaitable, propagating AnyIO cancellation back to the ROS action server.
- `action_server()` — wrap a standard `ActionServer`, translating ROS cancellation requests to AnyIO cancellation of the goal server callback.
- Utilities such as `future_result()`, `server_ready()`, `goal_status_str()`, and `goal_uuid_str()` for common integration tasks.

Everything runs on AnyIO, so you can stick with asyncio (the default backend) or switch to Trio/Curio by changing only your runner.

## Installation

Prerequisites: a ROS 2 workspace with `rclpy` available (e.g. Humble). Then install from PyPI:

```bash
pip install rclpy_async
```

Or install this repository in editable mode:

```bash
pip install -e .
```

## Getting Started

```python
import anyio
import rclpy
import turtlesim.srv

import rclpy_async

service_name = "/turtle1/teleport_relative"
request = turtlesim.srv.TeleportRelative.Request(linear=2.0, angular=1.57)

async def main():
	rclpy.init()
	node = rclpy.create_node("teleport_demo")
	async with rclpy_async.start_executor() as executor:
		executor.add_node(node)
		with rclpy_async.service_client(
			node, turtlesim.srv.TeleportRelative, service_name
		) as teleport:
			print(f"Teleport request: {request}")
			response = await teleport(request)
			print(f"Teleport response: {response}")


anyio.run(main)
```

Key points:

- `start_executor()` yields an `AsyncExecutor`. Add as many nodes as you like; they stay active until the context exits. The executor runs the ROS wait loop in its own thread.
- `start_executor()` also creates an AnyIO task group. The executor schedules nodes callbacks in the task group, so you can safely `await` and use AnyIO primitives inside the callbacks.


## Service and Action Helpers

### Service Client

```python
with rclpy_async.service_client(node, ExampleSrv, "/example") as call:
	reply = await call(ExampleSrv.Request())
```

The helper waits for the server, applies an AnyIO timeout, and cleans up the client automatically.

### Action Client

```python
with rclpy_async.action_client(node, ExampleAction, "/example") as send_goal:
	status, result = await send_goal(ExampleAction.Goal())
```

Cancelling the surrounding AnyIO scope triggers `cancel_goal_async()` and re-raises the cancellation so your caller still sees the cancel.

### Action Server

```python
with rclpy_async.action_server(
	node,
	ExampleAction,
	"/example",
	execute_callback=my_execute,
):
	await anyio.sleep_forever()
```

When `my_execute` runs under an AnyIO `CancelScope`, incoming client cancellation requests cancel the scope and let you tidy up before reporting the goal state.

## Utilities

These helpers smooth over common `rclpy` pain points when mixing with async code:

- `future_result(future)` — await an `rclpy.Future` inside AnyIO code without blocking the event loop.
- `server_ready(check_fn, timeout=5.0)` — poll for service/action availability.
- `goal_status_str(status)` / `goal_uuid_str(uuid)` — render action status codes and goal IDs for logging.

## Examples

The `examples/` directory contains runnable scripts that mirror real ROS 2 workflows:

- `service_call.py` — teleport a turtlesim turtle while consuming sensor updates.
- `action_call.py` / `action_call_cancel.py` — send goals and react to cancellation.
- `service_server.py` / `service_server_chain.py` — implement services with structured cancellation.
- `subscribe_next_update.py` — bridge subscription callbacks into AnyIO streams.

Run an example (with `ros2 run turtlesim turtlesim_node` in another terminal):

```bash
python examples/service_call.py
```

## Contributing

PRs welcome! Please keep examples tight, add tests when possible, and follow the repository’s `LICENSE` and Code of Conduct.

