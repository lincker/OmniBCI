"""Run the API normally, or as a privately controlled desktop sidecar."""
import argparse
import asyncio
import json
import multiprocessing
import os
import socket
import sys
import threading

_CONTROL_LIMIT = 65536
_WINDOWS_JOB = None


def _read_control(stream):
    line = stream.readline(_CONTROL_LIMIT + 1)
    if not line:
        return None
    if len(line) > _CONTROL_LIMIT or not line.endswith("\n"):
        raise ValueError("Invalid desktop control message")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Desktop control message must be an object")
    return message


def _bootstrap(stream):
    message = _read_control(stream)
    if message is None or message.get("op") != "bootstrap":
        raise ValueError("Desktop bootstrap is required")
    token, launch_id = message.get("token"), message.get("launch_id")
    if (not isinstance(token, str) or not 32 <= len(token) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise ValueError("Desktop bootstrap requires a strong ASCII token")
    if not isinstance(launch_id, str) or not launch_id or len(launch_id) > 128:
        raise ValueError("Desktop bootstrap requires a launch identifier")
    return token, launch_id


async def _desktop_server(token, launch_id, control_input, control_output):
    import uvicorn
    from omnibci.presentation.main import create_app

    app = create_app(api_token=token, desktop=True)
    loop = asyncio.get_running_loop()
    messages = asyncio.Queue()

    def emit(message):
        control_output.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
        control_output.flush()

    def progress(phase):
        try:
            emit({"event": "shutdown_progress", "phase": phase})
        except (OSError, ValueError):
            # A parent that has exited cannot receive progress; cleanup continues.
            pass

    app.state.shutdown_progress = progress

    def read_messages():
        try:
            while True:
                message = _read_control(control_input)
                loop.call_soon_threadsafe(messages.put_nowait, message)
                if message is None:
                    return
        except (ValueError, OSError) as exc:
            try:
                loop.call_soon_threadsafe(messages.put_nowait, {"control_error": str(exc)})
            except RuntimeError:
                pass

    # A daemon reader must not keep Python's default executor alive on quit.
    threading.Thread(target=read_messages, name="desktop-control", daemon=True).start()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, access_log=False, log_level="info",
            timeout_graceful_shutdown=10,
        ))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        incoming = asyncio.create_task(messages.get())
        shutdown_id = None
        parent_gone = False
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Backend failed to start")
                if incoming.done():
                    message = incoming.result()
                    if message is None or "control_error" in message:
                        parent_gone = True
                        return
                    raise ValueError("Wait for ready before sending desktop commands")
                await asyncio.sleep(0.01)
            emit({"event": "ready", "url": f"http://127.0.0.1:{port}",
                  "launch_id": launch_id, "instance_id": app.state.instance_id})
            while True:
                done, _ = await asyncio.wait({incoming, serving}, return_when=asyncio.FIRST_COMPLETED)
                if serving in done:
                    await serving
                    break
                message = incoming.result()
                if message is None or "control_error" in message:
                    parent_gone = True
                    break
                operation, identifier = message.get("op"), message.get("id")
                if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
                    raise ValueError("Desktop control requires a request identifier")
                if operation == "shutdown":
                    shutdown_id = identifier
                    break
                if operation == "prepare_shutdown":
                    progress("waiting_for_requests")
                    await app.state.request_gate.prepare()
                    errors = await app.state.shutdown_service.prepare(progress=progress)
                    emit({"event": "result", "id": identifier, "ok": not errors,
                          "errors": errors})
                elif operation == "resume":
                    app.state.shutdown_service.resume()
                    app.state.request_gate.prepared = False
                    emit({"event": "result", "id": identifier, "ok": True})
                else:
                    emit({"event": "result", "id": identifier, "ok": False,
                          "errors": ["Unknown desktop operation"]})
                incoming = asyncio.create_task(messages.get())
        finally:
            incoming.cancel()
            server.should_exit = True
            await serving
            if shutdown_id is not None and not parent_gone:
                errors = list(app.state.shutdown_errors)
                if not app.state.shutdown_complete and not errors:
                    errors.append("Backend lifespan did not shut down cleanly; inspect the backend log")
                emit({"event": "result", "id": shutdown_id, "ok": not errors, "errors": errors})


def main(argv=None):
    global _WINDOWS_JOB
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Run the OmniBCI API backend")
    parser.add_argument("--desktop", action="store_true", help="Use private desktop control over stdin/stdout")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not args.desktop:
        import uvicorn
        from omnibci.presentation.main import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return
    # Keep a private copy of the pipe, then redirect even native/plugin writes
    # to fd 1. Spawned workers therefore cannot corrupt protocol stdout.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1) as protocol_output:
        sys.stdout.flush()
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        token, launch_id = _bootstrap(sys.stdin)
        from omnibci.infrastructure.process_tree import own_windows_process_tree
        _WINDOWS_JOB = own_windows_process_tree()
        asyncio.run(_desktop_server(token, launch_id, sys.stdin, protocol_output))


if __name__ == "__main__":
    main()
