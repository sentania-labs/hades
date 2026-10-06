"""FastAPI router that keeps complete request handlers off the event loop."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from fastapi import APIRouter


class ThreadedAPIRouter(APIRouter):
    """Register coroutine handlers as synchronous threadpool entry points.

    Hades uses synchronous SQLAlchemy units of work. Keeping the wrapper synchronous
    makes Starlette run dependency-backed handlers in its threadpool. Async provider
    work still works on a private loop in that worker thread.
    """

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if inspect.iscoroutinefunction(endpoint):
            async_endpoint = endpoint

            def threaded(*args: Any, **call_kwargs: Any) -> Any:
                return asyncio.run(async_endpoint(*args, **call_kwargs))

            threaded.__name__ = async_endpoint.__name__
            threaded.__doc__ = async_endpoint.__doc__
            threaded.__module__ = async_endpoint.__module__
            hints = get_type_hints(async_endpoint, include_extras=True)
            signature = inspect.signature(async_endpoint)
            parameters = [
                parameter.replace(annotation=hints.get(name, parameter.annotation))
                for name, parameter in signature.parameters.items()
            ]
            threaded.__annotations__ = hints
            threaded.__signature__ = signature.replace(  # type: ignore[attr-defined]
                parameters=parameters,
                return_annotation=hints.get("return", signature.return_annotation),
            )
            endpoint = threaded
        super().add_api_route(path, endpoint, **kwargs)
