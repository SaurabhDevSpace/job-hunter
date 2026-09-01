"""Minimal FastAPI-compatible shim built on Starlette.

Why this exists
---------------
This machine's endpoint-security agent refuses to ``mmap`` the Rust-compiled
``pydantic_core`` shared library (``dlopen ... errno=1``), which makes FastAPI
impossible to import locally. Every pydantic v2 release is affected, and
pydantic v1 does not work on Python 3.14.

The dashboard only ever used the small, declarative subset of FastAPI listed
below — no ``BaseModel``, no ``Depends``, no ``response_model``. That subset maps
cleanly onto Starlette, which is pure Python and loads fine.

``jobhunter.web.app`` imports the real FastAPI when it is available and falls
back to this module otherwise, so nothing changes on machines without the
restriction.

Supported: path/query parameter injection with type coercion, ``Request``,
``BackgroundTasks``, ``HTTPException``, ``Query`` defaults, ``on_event``,
``mount``, and ``response_class``.
"""
from __future__ import annotations

import contextlib
import inspect
import typing
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.background import BackgroundTasks
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

__all__ = [
    "BackgroundTasks", "FastAPI", "FileResponse", "HTMLResponse", "HTTPException",
    "JSONResponse", "Jinja2Templates", "PlainTextResponse", "Query", "Request",
    "Response", "StaticFiles",
]

_UNSET = object()


def Query(default: Any = _UNSET, **_kwargs: Any) -> Any:
    """FastAPI's ``Query(...)`` marker reduced to "just the default value"."""
    return None if default is _UNSET else default


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _coerce(raw: str, annotation: Any) -> Any:
    """Convert a raw query/path string using a possibly-stringified annotation.

    ``app.py`` uses ``from __future__ import annotations``, so annotations arrive
    as strings such as ``"str | None"``. Unknown types are passed through as-is.
    """
    if annotation is inspect.Parameter.empty:
        return raw

    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    # Reduce "str | None" / "Optional[int]" to the underlying scalar name.
    name = name.replace("Optional[", "").replace("]", "")
    parts = [p.strip() for p in name.split("|") if p.strip() and p.strip() != "None"]
    base = parts[0].lower() if parts else ""

    try:
        if base == "bool":
            return _truthy(raw)
        if base == "int":
            return int(raw)
        if base == "float":
            return float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid value: {raw!r}") from exc
    return raw


def _is_type(annotation: Any, target: type) -> bool:
    if annotation is target:
        return True
    return isinstance(annotation, str) and annotation.strip() == target.__name__


class FastAPI(Starlette):
    """The slice of the FastAPI application API that the dashboard relies on."""

    def __init__(self, *, title: str = "", version: str = "", **kwargs: Any) -> None:
        self._startup_handlers: list[Callable[..., Any]] = []
        self._shutdown_handlers: list[Callable[..., Any]] = []
        kwargs.setdefault("lifespan", self._lifespan)
        super().__init__(**kwargs)
        self.title = title
        self.version = version

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: Any):
        # Starlette dropped add_event_handler, so on_event is backed by lifespan.
        await self._fire(self._startup_handlers)
        try:
            yield
        finally:
            await self._fire(self._shutdown_handlers)

    @staticmethod
    async def _fire(handlers: list[Callable[..., Any]]) -> None:
        for handler in handlers:
            result = handler()
            if inspect.isawaitable(result):
                await result

    # ------------------------------------------------------------ routing
    def _register(self, path: str, methods: list[str], response_class: type[Response]):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            endpoint = self._build_endpoint(func, response_class)
            self.router.routes.append(
                Route(path, endpoint, methods=methods, name=func.__name__)
            )
            return func

        return decorator

    def get(self, path: str, *, response_class: type[Response] = JSONResponse, **_kw):
        return self._register(path, ["GET"], response_class)

    def post(self, path: str, *, response_class: type[Response] = JSONResponse, **_kw):
        return self._register(path, ["POST"], response_class)

    def put(self, path: str, *, response_class: type[Response] = JSONResponse, **_kw):
        return self._register(path, ["PUT"], response_class)

    def delete(self, path: str, *, response_class: type[Response] = JSONResponse, **_kw):
        return self._register(path, ["DELETE"], response_class)

    # ------------------------------------------------------ event handlers
    def on_event(self, event_type: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if event_type == "startup":
                self._startup_handlers.append(func)
            elif event_type == "shutdown":
                self._shutdown_handlers.append(func)
            else:
                raise ValueError(f"unsupported event type: {event_type}")
            return func

        return decorator

    # --------------------------------------------------- request handling
    @staticmethod
    def _build_endpoint(func: Callable[..., Any], response_class: type[Response]):
        signature = inspect.signature(func)
        is_async = inspect.iscoroutinefunction(func)

        async def endpoint(request: Request) -> Response:
            kwargs: dict[str, Any] = {}
            tasks: BackgroundTasks | None = None

            for name, param in signature.parameters.items():
                annotation = param.annotation

                if _is_type(annotation, Request):
                    kwargs[name] = request
                    continue
                if _is_type(annotation, BackgroundTasks):
                    tasks = tasks or BackgroundTasks()
                    kwargs[name] = tasks
                    continue

                if name in request.path_params:
                    kwargs[name] = _coerce(request.path_params[name], annotation)
                    continue

                raw = request.query_params.get(name)
                if raw is not None:
                    kwargs[name] = _coerce(raw, annotation)
                elif param.default is not inspect.Parameter.empty:
                    kwargs[name] = param.default
                else:
                    raise HTTPException(422, f"missing required parameter: {name}")

            if is_async:
                result = await func(**kwargs)
            else:
                # Handlers do blocking SQLite/network work, so keep them off the loop.
                result = await run_in_threadpool(func, **kwargs)

            response = result if isinstance(result, Response) else _to_response(
                result, response_class
            )
            if tasks is not None and tasks.tasks:
                response.background = tasks
            return response

        return endpoint


def _to_response(result: Any, response_class: type[Response]) -> Response:
    if result is None:
        return Response(status_code=204)
    if isinstance(result, (dict, list)):
        return JSONResponse(result)
    if isinstance(result, str) and issubclass(response_class, (HTMLResponse, PlainTextResponse)):
        return response_class(result)
    return JSONResponse(result)


def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Match FastAPI's ``{"detail": ...}`` error body, which the UI expects."""
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# Mirror `fastapi.responses` so `from ... import responses` style access works.
class _Responses:
    FileResponse = FileResponse
    HTMLResponse = HTMLResponse
    JSONResponse = JSONResponse
    PlainTextResponse = PlainTextResponse
    Response = Response


responses = _Responses()

if typing.TYPE_CHECKING:  # pragma: no cover
    pass
