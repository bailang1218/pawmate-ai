"""Authenticated loopback HTTP API for testing browser automation before UI integration."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import logging
import os
import secrets
from typing import Any, Awaitable, Callable

from aiohttp import web

from .service import AutomationServiceError, BrowserAutomationService
from .validation import (
    ALLOWED_ACTIONS,
    MAX_ITERATIONS,
    MAX_NESTING,
    MAX_STEPS,
    MAX_TIMEOUT_MS,
    MAX_WORKFLOW_BYTES,
)


API_VERSION = "1.0"
DEFAULT_PORT = 18766
MAX_BODY_BYTES = 256 * 1024
AUTOMATION_SERVICE_KEY = web.AppKey("automation_service", BrowserAutomationService)


def create_app(service: BrowserAutomationService | None = None, *, token: str) -> web.Application:
    if len(token) < 24:
        raise ValueError("API token must contain at least 24 characters")

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path == "/health":
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = request.headers.get("X-PawMate-Token", "").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            raise web.HTTPUnauthorized(
                text=_json_text({"ok": False, "error": {"code": "unauthorized", "message": "Valid bearer token required"}}),
                content_type="application/json",
            )
        return await handler(request)

    app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[auth_middleware])
    app[AUTOMATION_SERVICE_KEY] = service or BrowserAutomationService()
    app.on_cleanup.append(_cleanup_service)
    app.router.add_get("/health", _health)
    app.router.add_get("/v1/capabilities", _capabilities)
    app.router.add_post("/v1/workflows/validate", _validate_workflow)
    app.router.add_post("/v1/workflows/generate", _generate_workflow)
    app.router.add_post("/v1/workflows", _create_workflow)
    app.router.add_get("/v1/workflows", _list_workflows)
    app.router.add_get("/v1/workflows/{workflow_id}", _get_workflow)
    app.router.add_put("/v1/workflows/{workflow_id}", _update_workflow)
    app.router.add_get("/v1/workflows/{workflow_id}/versions/{version}", _get_workflow_version)
    app.router.add_post("/v1/workflows/{workflow_id}/runs", _start_workflow_run)
    app.router.add_post("/v1/agent/runs", _start_agent_run)
    app.router.add_get("/v1/runs", _list_runs)
    app.router.add_get("/v1/runs/{run_id}", _get_run)
    app.router.add_post("/v1/runs/{run_id}/cancel", _cancel_run)
    app.router.add_get("/v1/runs/{run_id}/trace", _get_trace)
    return app


async def _cleanup_service(app: web.Application) -> None:
    await app[AUTOMATION_SERVICE_KEY].close()


async def _health(request: web.Request) -> web.Response:
    service = _service(request)
    return _ok(
        {
            "service": "pawmate-browser-automation",
            "version": API_VERSION,
            "busy": service.busy,
            "modes": ["agent", "workflow", "ai_generated_workflow"],
        }
    )


async def _capabilities(request: web.Request) -> web.Response:
    return _ok(
        {
            "api_version": API_VERSION,
            "actions": sorted(ALLOWED_ACTIONS),
            "limits": {
                "workflow_bytes": MAX_WORKFLOW_BYTES,
                "steps": MAX_STEPS,
                "nesting": MAX_NESTING,
                "iterations": MAX_ITERATIONS,
                "step_timeout_ms": MAX_TIMEOUT_MS,
                "request_body_bytes": MAX_BODY_BYTES,
            },
            "risk_defaults": {
                "high_requires_approval": True,
                "critical_requires_scoped_approval": True,
                "model_data_requires_consent": True,
            },
        }
    )


async def _validate_workflow(request: web.Request) -> web.Response:
    body = await _json_body(request)
    return _ok(_service(request).validate_workflow(body))


async def _generate_workflow(request: web.Request) -> web.Response:
    body = await _json_body(request)
    goal = str(body.get("goal") or "")
    context = body.get("context") or {}
    if not isinstance(context, dict):
        raise web.HTTPUnprocessableEntity(text=_error_text("invalid_context", "context must be an object"), content_type="application/json")
    try:
        result = await _service(request).generate_workflow(
            goal,
            context,
            save=bool(body.get("save", False)),
            allow_model_data=body.get("allow_model_data") is True,
        )
        return _ok(result)
    except Exception as exc:
        return _handle_error(exc)


async def _create_workflow(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        workflow = _service(request).create_workflow(body)
        return _ok({"workflow": workflow.to_dict()}, status=201)
    except Exception as exc:
        return _handle_error(exc)


async def _update_workflow(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        workflow = _service(request).update_workflow(request.match_info["workflow_id"], body)
        return _ok({"workflow": workflow.to_dict()})
    except Exception as exc:
        return _handle_error(exc)


async def _list_workflows(request: web.Request) -> web.Response:
    workflows = [workflow.to_dict() for workflow in _service(request).list_workflows()]
    return _ok({"workflows": workflows, "count": len(workflows)})


async def _get_workflow(request: web.Request) -> web.Response:
    try:
        workflow = _service(request).get_workflow(request.match_info["workflow_id"])
        return _ok({"workflow": workflow.to_dict()})
    except Exception as exc:
        return _handle_error(exc)


async def _get_workflow_version(request: web.Request) -> web.Response:
    try:
        workflow = _service(request).get_workflow_version(
            request.match_info["workflow_id"],
            int(request.match_info["version"]),
        )
        return _ok({"workflow": workflow.to_dict()})
    except Exception as exc:
        return _handle_error(exc)


async def _start_workflow_run(request: web.Request) -> web.Response:
    body = await _json_body(request)
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise web.HTTPUnprocessableEntity(text=_error_text("invalid_inputs", "inputs must be an object"), content_type="application/json")
    try:
        run = await _service(request).start_workflow_run(
            request.match_info["workflow_id"],
            inputs=inputs,
            dry_run=bool(body.get("dry_run", False)),
            allow_high_risk=body.get("allow_high_risk") is True,
            allow_critical=body.get("allow_critical") is True,
            approval=body.get("approval"),
        )
        return _ok({"run": run.to_dict()}, status=202)
    except Exception as exc:
        return _handle_error(exc)


async def _start_agent_run(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        run = await _service(request).start_agent_run(
            str(body.get("goal") or ""),
            max_turns=int(body.get("max_turns", 20)),
            step_timeout_ms=int(body.get("step_timeout_ms", 30_000)),
            allow_high_risk=body.get("allow_high_risk") is True,
            allow_critical=body.get("allow_critical") is True,
            allow_model_data=body.get("allow_model_data") is True,
            approval=body.get("approval"),
        )
        return _ok({"run": run.to_dict()}, status=202)
    except Exception as exc:
        return _handle_error(exc)


async def _get_run(request: web.Request) -> web.Response:
    try:
        run = _service(request).get_run(request.match_info["run_id"])
        return _ok({"run": run.to_dict()})
    except Exception as exc:
        return _handle_error(exc)


async def _list_runs(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
        runs = [item.to_dict() for item in _service(request).list_runs(limit=limit)]
        return _ok({"runs": runs, "count": len(runs)})
    except Exception as exc:
        return _handle_error(exc)


async def _cancel_run(request: web.Request) -> web.Response:
    try:
        accepted = _service(request).cancel_run(request.match_info["run_id"])
        return _ok({"accepted": accepted}, status=202 if accepted else 409)
    except Exception as exc:
        return _handle_error(exc)


async def _get_trace(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "1000"))
        trace = _service(request).get_trace(request.match_info["run_id"], limit=limit)
        return _ok({"trace": trace, "count": len(trace)})
    except (TypeError, ValueError):
        return _ok({"ok": False, "error": {"code": "invalid_limit", "message": "limit must be an integer"}}, status=422)
    except Exception as exc:
        return _handle_error(exc)


async def _json_body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text=_error_text("content_type", "Content-Type must be application/json"), content_type="application/json")
    try:
        value = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=_error_text("invalid_json", "Request body must be valid JSON"), content_type="application/json") from exc
    if not isinstance(value, dict):
        raise web.HTTPUnprocessableEntity(text=_error_text("invalid_body", "JSON body must be an object"), content_type="application/json")
    return value


def _service(request: web.Request) -> BrowserAutomationService:
    return request.app[AUTOMATION_SERVICE_KEY]


def _handle_error(exc: Exception) -> web.Response:
    if isinstance(exc, AutomationServiceError):
        payload: dict[str, Any] = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        if exc.details is not None:
            payload["error"]["details"] = exc.details
        return web.json_response(payload, status=exc.http_status)
    if isinstance(exc, (TypeError, ValueError)):
        return web.json_response({"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}, status=422)
    logging.getLogger("pawmate.browser_automation.api").exception("Browser automation API request failed", exc_info=exc)
    return web.json_response(
        {"ok": False, "error": {"code": "internal_error", "message": "Internal browser automation error"}},
        status=500,
    )


def _ok(data: dict[str, Any], *, status: int = 200) -> web.Response:
    payload = data if "ok" in data else {"ok": True, **data}
    return web.json_response(payload, status=status)


def _json_text(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _error_text(code: str, message: str) -> str:
    return _json_text({"ok": False, "error": {"code": code, "message": message}})


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="PawMate browser automation test API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        parser.error("Refusing to bind browser automation API to a non-loopback host")
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    token = args.token or os.environ.get("PAWMATE_AUTOMATION_TOKEN", "") or secrets.token_urlsafe(32)
    if len(token) < 24:
        parser.error("API token must contain at least 24 characters")
    print(f"PawMate browser automation API: http://{args.host}:{args.port}")
    if not args.token and not os.environ.get("PAWMATE_AUTOMATION_TOKEN"):
        print(f"One-time bearer token: {token}")
    web.run_app(create_app(token=token), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
