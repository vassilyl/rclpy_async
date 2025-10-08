# rclpy_async

Bridge core ROS 2 client library (rclpy) primitives into [structured concurrency](https://vorpus.org/blog/notes-on-structured-concurrency-or-go-statement-considered-harmful/)
with [AnyIO](https://anyio.readthedocs.io/en/stable/index.html) (asyncio, Trio, Curio–style unified API). Write ROS 2 actions, services, and subscriptions using native `async/await` without spinning your own executor in the foreground.

## Why

`rclpy` provides asynchronous primitives but still requires managing an executor thread, callback groups, and thread ↔ event‑loop handoff. `rclpy_async` embeds a ROS 2 `SingleThreadedExecutor` in a background thread and exposes a small async façade (`NodeAsync`) that:

- Initializes / shuts down ROS 2 automatically
- Translates ROS callbacks into AnyIO tasks
- Provides context managers for subscriptions, services, and actions
- Propagates structured cancellation (cancelling an AnyIO scope cancels an action goal)

Works with AnyIO, so your code can run on the default asyncio backend (or Trio) without changes.

## Installation

Prerequisites: A working ROS 2 (e.g. Humble) environment (`rclpy` available in your Python). Then install:

```bash
pip install rclpy_async
```

Or, from source (in this repo):

```bash
pip install -e .
```

## Quick Start Snippets (≤5 lines each)

All snippets assume: `from rclpy_async import NodeAsync` and `import anyio` plus relevant message/action types.

### Create a Node
```python
async with NodeAsync("demo") as anode:
		...
```

### Subscription (receive one message)
```python
send, recv = anyio.create_memory_object_stream(0)
with anode.subscription(Pose, "/turtle1/pose", send.send_nowait, qos_profile=0):
	msg = await recv.receive()
```

See [`subscribe_next_update.py`](examples/subscribe_next_update.py) for a complete example.

### Service Client
```python
async with anode.service_client(TeleportRelative, "/turtle1/teleport_relative") as call:
	resp = await call(TeleportRelative.Request(linear=2.0, angular=1.57))
```
See [`service_call.py`](examples/service_call.py) for a complete example.

### Action Client
```python
async with node.action_client(RotateAbsolute, "/turtle1/rotate_absolute") as send:
	status, result = await send(RotateAbsolute.Goal(theta=3.14))
```

See [`action_call.py`](examples/action_call.py)

### Action With Feedback and Cancellation
```python
scope = anyio.CancelScope()

async def fb(msg):
	if abs(msg.feedback.remaining) < 1: scope.cancel()

async with anode.action_client(
	RotateAbsolute, "/turtle1/rotate_absolute", feedback_handler_async=fb
) as send:
	with scope: await send(RotateAbsolute.Goal(theta=2.0))
```

See [`action_call_cancel.py`](examples/action_call_cancel.py) for a complete example.

## Examples

See the `examples/` directory for complete flows:

- `turtlesim_sample.py` – mixed subscription, service, action (with feedback)
- `action_call.py` – minimal RotateAbsolute action call
- `action_call_cancel.py` – cancellation via feedback threshold
- `subscribe_action_status.py` – subscribing to action status updates (QoS transient local)
- `service_call.py` – simple service invocation

Run any example (requires running `turtlesim_node`):
```bash
ros2 run turtlesim turtlesim_node   # separate terminal
python examples/action_call.py
```

Hint: to run turtlesim_node without GUI: `ros2 run turtlesim turtlesim_node --platform offscreen`

## API Surface (Condensed)

```python
async with NodeAsync(name, *, args=None, namespace=None, domain_id=None)
with node.subscription(msg_type, topic, callback, qos_profile)
async with node.service_client(SrvType, name) -> async (request)->response
async with node.action_client(ActionType, name, feedback_handler_async=None) -> async (goal)->(status,result)
```

All context managers are cancel‑aware and guarantee resource cleanup on exit.

## Cancellation Semantics

Cancelling a task awaiting an action goal triggers a best‑effort `cancel_goal_async()` and re‑raises the cancellation so outer scopes see normal cancellation behavior. Feedback handlers run in the AnyIO event loop thread via the portal.

## Contributing

PRs welcome. Please ensure style is minimal and examples remain concise. See `LICENSE` for license terms and Code of Conduct below.

