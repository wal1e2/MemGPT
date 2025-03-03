import asyncio
import json
import os
import warnings
from enum import Enum
from typing import TYPE_CHECKING, AsyncGenerator, Optional, Union

from fastapi import Header
from pydantic import BaseModel

from letta.errors import ContextWindowExceededError, RateLimitExceededError
from letta.schemas.usage import LettaUsageStatistics
from letta.server.rest_api.interface import StreamingServerInterface

if TYPE_CHECKING:
    from letta.server.server import SyncServer

# from letta.orm.user import User
# from letta.orm.utilities import get_db_session

SSE_PREFIX = "data: "
SSE_SUFFIX = "\n\n"
SSE_FINISH_MSG = "[DONE]"  # mimic openai
SSE_ARTIFICIAL_DELAY = 0.1


def sse_formatter(data: Union[dict, str]) -> str:
    """Prefix with 'data: ', and always include double newlines"""
    assert type(data) in [dict, str], f"Expected type dict or str, got type {type(data)}"
    data_str = json.dumps(data, separators=(",", ":")) if isinstance(data, dict) else data
    return f"data: {data_str}\n\n"


async def sse_async_generator(
    generator: AsyncGenerator,
    usage_task: Optional[asyncio.Task] = None,
    finish_message=True,
):
    """
    Wraps a generator for use in Server-Sent Events (SSE), handling errors and ensuring a completion message.

    Args:
    - generator: An asynchronous generator yielding data chunks.

    Yields:
    - Formatted Server-Sent Event strings.
    """
    try:
        async for chunk in generator:
            # yield f"data: {json.dumps(chunk)}\n\n"
            if isinstance(chunk, BaseModel):
                chunk = chunk.model_dump()
            elif isinstance(chunk, Enum):
                chunk = str(chunk.value)
            elif not isinstance(chunk, dict):
                chunk = str(chunk)
            yield sse_formatter(chunk)

        # If we have a usage task, wait for it and send its result
        if usage_task is not None:
            try:
                usage = await usage_task
                # Double-check the type
                if not isinstance(usage, LettaUsageStatistics):
                    raise ValueError(f"Expected LettaUsageStatistics, got {type(usage)}")
                yield sse_formatter(usage.model_dump())

            except ContextWindowExceededError as e:
                log_error_to_sentry(e)
                yield sse_formatter({"error": f"Stream failed: {e}", "code": str(e.code.value) if e.code else None})

            except RateLimitExceededError as e:
                log_error_to_sentry(e)
                yield sse_formatter({"error": f"Stream failed: {e}", "code": str(e.code.value) if e.code else None})

            except Exception as e:
                log_error_to_sentry(e)
                yield sse_formatter({"error": f"Stream failed (internal error occured)"})

    except Exception as e:
        log_error_to_sentry(e)
        yield sse_formatter({"error": "Stream failed (decoder encountered an error)"})

    finally:
        if finish_message:
            # Signal that the stream is complete
            yield sse_formatter(SSE_FINISH_MSG)


# TODO: why does this double up the interface?
def get_letta_server() -> "SyncServer":
    # Check if a global server is already instantiated
    from letta.server.rest_api.app import server

    # assert isinstance(server, SyncServer)
    return server


# Dependency to get user_id from headers
def get_user_id(user_id: Optional[str] = Header(None, alias="user_id")) -> Optional[str]:
    return user_id


def get_current_interface() -> StreamingServerInterface:
    return StreamingServerInterface


def log_error_to_sentry(e):
    import traceback

    traceback.print_exc()
    warnings.warn(f"SSE stream generator failed: {e}")

    # Log the error, since the exception handler upstack (in FastAPI) won't catch it, because this may be a 200 response
    # Print the stack trace
    if (os.getenv("SENTRY_DSN") is not None) and (os.getenv("SENTRY_DSN") != ""):
        import sentry_sdk

        sentry_sdk.capture_exception(e)
