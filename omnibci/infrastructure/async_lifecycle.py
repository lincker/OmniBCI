"""Finish resource-owning work before propagating caller cancellation."""
import asyncio


async def await_owned(future):
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError as exc:
            if (future.cancelled()
                    or future.done() and isinstance(future.exception(), asyncio.CancelledError)):
                raise
            cancellation = cancellation or exc
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
    if cancellation is not None:
        raise cancellation
    return result
