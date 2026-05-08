#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import argparse
import asyncio
import os
from functools import wraps
from typing import Any, Callable, List, Literal, Optional, Type

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .resources.cloudwatch_logs_resource import CloudWatchLogsResource
from .tools.search_tools import CloudWatchLogsSearchTools
from .tools.analysis_tools import CloudWatchLogsAnalysisTools
from .tools.correlation_tools import CloudWatchLogsCorrelationTools

# This module registers CloudWatch MCP resources/tools and supports both local stdio
# transport and remote streamable HTTP transport for deployment environments.

# Parse command line arguments
parser = argparse.ArgumentParser(description="CloudWatch Logs Analyzer MCP Server")
parser.add_argument(
    "--profile", type=str, help="AWS profile name to use for credentials"
)
parser.add_argument("--region", type=str, help="AWS region name to use for API calls")
parser.add_argument(
    "--transport",
    choices=["stdio", "streamable-http", "sse"],
    default="stdio",
    help="MCP transport mode (default: stdio)",
)
parser.add_argument(
    "--host",
    type=str,
    default="0.0.0.0",
    help="Host interface for HTTP/SSE transports (default: 0.0.0.0)",
)
parser.add_argument(
    "--port",
    type=int,
    default=8000,
    help="Port for HTTP/SSE transports (default: 8000)",
)
parser.add_argument(
    "--streamable-http-path",
    type=str,
    default="/mcp",
    help="HTTP route path when using streamable-http transport (default: /mcp)",
)
parser.add_argument(
    "--stateless", action="store_true", help="Stateless HTTP mode", default=False
)
parser.add_argument(
    "--trusted-host",
    action="append",
    default=["*"],
    help="Allowed host pattern for TrustedHostMiddleware; repeat for multiple values",
)
parser.add_argument(
    "--auth-token-env",
    type=str,
    default="MCP_AUTH_TOKEN",
    help=(
        "Environment variable name that holds the shared bearer token for "
        "x-mcp-token header validation. Auth is enabled only if this env var is set "
        "(default name: MCP_AUTH_TOKEN)"
    ),
)
parser.add_argument(
    "--auth-header-name",
    type=str,
    default="x-mcp-token",
    help="HTTP header name carrying the shared bearer token (default: x-mcp-token)",
)
args, unknown = parser.parse_known_args()


class TokenAuthMiddleware:
    """Pure ASGI middleware that validates a shared bearer token header.

    Implemented as raw ASGI to remain compatible with streaming responses
    (SSE / chunked transfer) used by FastMCP's streamable-HTTP transport.
    Requests missing or with an invalid token are rejected with HTTP 401.
    """

    def __init__(self, app: Any, header_name: str, expected_token: str) -> None:
        self._app = app
        self._header_name = header_name.lower().encode("latin-1")
        self._expected_token = expected_token.encode("latin-1")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """Inspect the token header and forward or reject the request."""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        provided_token: Optional[bytes] = None
        for header_name, header_value in scope.get("headers", []):
            if header_name == self._header_name:
                provided_token = header_value
                break

        if not provided_token or provided_token != self._expected_token:
            response = JSONResponse(
                {"error": "unauthorized", "reason": "invalid_or_missing_token"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


class SecureFastMCP(FastMCP):
    """FastMCP extension that adds host filtering and optional token auth."""

    def __init__(
        self,
        *mcp_args: Any,
        trusted_hosts: List[str],
        auth_header_name: Optional[str] = None,
        auth_token: Optional[str] = None,
        **mcp_kwargs: Any,
    ) -> None:
        super().__init__(*mcp_args, **mcp_kwargs)
        self._trusted_hosts = trusted_hosts
        self._auth_header_name = auth_header_name
        self._auth_token = auth_token

    def _apply_security_middlewares(self, app: Any) -> Any:
        """Apply trusted-host and optional token-auth middlewares to the app."""
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=self._trusted_hosts)
        if self._auth_token and self._auth_header_name:
            app.add_middleware(
                TokenAuthMiddleware,
                header_name=self._auth_header_name,
                expected_token=self._auth_token,
            )
        return app

    def streamable_http_app(self) -> Any:
        """Build streamable HTTP app with security middlewares applied."""
        app = super().streamable_http_app()
        return self._apply_security_middlewares(app)

    def sse_app(self) -> Any:
        """Build SSE app with security middlewares applied."""
        app = super().sse_app()
        return self._apply_security_middlewares(app)


# Resolve optional shared bearer token from environment for HTTP transports.
# If the env var is unset, token-based auth is disabled and the server keeps
# its previous behavior (host-header validation only).
configured_auth_token = os.getenv(args.auth_token_env)

# Create the MCP server for CloudWatch logs
# Host validation is handled by TrustedHostMiddleware at the outer Starlette layer,
# so FastMCP's internal DNS rebinding protection is disabled here to avoid double-validation.
mcp = SecureFastMCP(
    "CloudWatch Logs Analyzer",
    stateless_http=args.stateless,
    trusted_hosts=args.trusted_host,
    auth_header_name=args.auth_header_name,
    auth_token=configured_auth_token,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# Initialize our resource and tools classes with the specified AWS profile and region
cw_resource = CloudWatchLogsResource(profile_name=args.profile, region_name=args.region)
search_tools = CloudWatchLogsSearchTools(
    profile_name=args.profile, region_name=args.region
)
analysis_tools = CloudWatchLogsAnalysisTools(
    profile_name=args.profile, region_name=args.region
)
correlation_tools = CloudWatchLogsCorrelationTools(
    profile_name=args.profile, region_name=args.region
)

# Capture the parsed CLI profile and region in separate variables
default_profile = args.profile
default_region = args.region


# Helper decorator to handle profile and region parameters for tools
def with_aws_config(tool_class: Type, method_name: Optional[str] = None) -> Callable:
    """
    Decorator that handles the profile and region parameters for tool functions.
    Creates a new instance of the specified tool class with the correct profile and region.

    Args:
        tool_class: The class to instantiate with the profile and region
        method_name: Optional method name if different from the decorated function
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            try:
                profile = kwargs.pop("profile", None) or default_profile
                region = kwargs.pop("region", None) or default_region
                tool_instance = tool_class(profile_name=profile, region_name=region)
                target_method = method_name or func.__name__
                method = getattr(tool_instance, target_method)
                result = method(**kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            except AttributeError as e:
                raise RuntimeError(
                    f"Method {target_method} not found in {tool_class.__name__}, {e}"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    f"An error {e} occurred while executing {target_method} in {tool_class.__name__}"
                ) from e

        return wrapper

    return decorator


# ==============================
# Resource Handlers
# ==============================


@mcp.resource("logs://groups")
def get_log_groups() -> str:
    """Get a list of all CloudWatch Log Groups"""
    # Use default values for parameters
    prefix = None
    limit = 50
    next_token = None

    return cw_resource.get_log_groups(prefix, limit, next_token)


@mcp.resource("logs://groups/filter/{prefix}")
def get_filtered_log_groups(prefix: str) -> str:
    """
    Get a filtered list of CloudWatch Log Groups by prefix

    Args:
        prefix: The prefix to filter log groups by
    """
    # Default values for other parameters
    limit = 50
    next_token = None

    return cw_resource.get_log_groups(prefix, limit, next_token)


@mcp.resource("logs://groups/{log_group_name}")
def get_log_group_details(log_group_name: str) -> str:
    """Get detailed information about a specific log group"""
    return cw_resource.get_log_group_details(log_group_name)


@mcp.resource("logs://groups/{log_group_name}/streams")
def get_log_streams(log_group_name: str) -> str:
    """
    Get a list of log streams for a specific log group

    Args:
        log_group_name: The name of the log group
    """
    # Use default limit value
    limit = 20
    return cw_resource.get_log_streams(log_group_name, limit)


@mcp.resource("logs://groups/{log_group_name}/streams/{log_stream_name}")
def get_log_events(log_group_name: str, log_stream_name: str) -> str:
    """
    Get log events from a specific log stream

    Args:
        log_group_name: The name of the log group
        log_stream_name: The name of the log stream
    """
    # Use default limit value
    limit = 100
    return cw_resource.get_log_events(log_group_name, log_stream_name, limit)


@mcp.resource("logs://groups/{log_group_name}/sample")
def get_log_sample(log_group_name: str) -> str:
    """
    Get a sample of recent logs from a log group

    Args:
        log_group_name: The name of the log group
    """
    # Use default limit value
    limit = 10
    return cw_resource.get_log_sample(log_group_name, limit)


@mcp.resource("logs://groups/{log_group_name}/recent-errors")
def get_recent_errors(log_group_name: str) -> str:
    """
    Get recent error logs from a log group

    Args:
        log_group_name: The name of the log group
    """
    # Use default hours value
    hours = 24
    return cw_resource.get_recent_errors(log_group_name, hours)


@mcp.resource("logs://groups/{log_group_name}/metrics")
def get_log_metrics(log_group_name: str) -> str:
    """
    Get log volume metrics for a log group

    Args:
        log_group_name: The name of the log group
    """
    # Use default hours value
    hours = 24
    return cw_resource.get_log_metrics(log_group_name, hours)


@mcp.resource("logs://groups/{log_group_name}/structure")
def analyze_log_structure(log_group_name: str) -> str:
    """Analyze and provide information about the structure of logs"""
    return cw_resource.analyze_log_structure(log_group_name)


# ==============================
# Prompts
# ==============================


@mcp.prompt()
def list_cloudwatch_log_groups(
    prefix: str = None, profile: str = None, region: str = None
) -> str:
    """
    Prompt for listing and exploring CloudWatch log groups.

    Args:
        prefix: Optional prefix to filter log groups by name
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls
    """
    profile_text = f" using profile '{profile}'" if profile else ""
    region_text = f" in region '{region}'" if region else ""
    prefix_text = f" with prefix '{prefix}'" if prefix else ""

    return f"""I'll help you explore the CloudWatch log groups in your AWS environment{profile_text}{region_text}.

First, I'll list the available log groups{prefix_text}.

For each log group, I can help you:
1. Get detailed information about the group (retention, size, etc.)
2. Check for recent errors or patterns
3. View metrics like volume and activity
4. Sample recent logs to understand the content
5. Search for specific patterns or events

Let me know which log group you'd like to explore further, or if you'd like to refine the search with a different prefix.
"""


@mcp.prompt()
def analyze_cloudwatch_logs(
    log_group_name: str, profile: str = None, region: str = None
) -> str:
    """
    Prompt for analyzing CloudWatch logs to help identify issues, patterns, and insights.

    Args:
        log_group_name: The name of the log group to analyze
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls
    """
    profile_text = f" using profile '{profile}'" if profile else ""
    region_text = f" in region '{region}'" if region else ""

    return f"""Please analyze the following CloudWatch logs from the {log_group_name} log group{profile_text}{region_text}.

First, I'll get you some information about the log group:
1. Get the basic log group structure to understand the format of logs
2. Check for any recent errors
3. Examine the log volume metrics
4. Analyze a sample of recent logs

Based on this information, please:
- Identify any recurring errors or exceptions
- Look for unusual patterns or anomalies
- Suggest possible root causes for any issues found
- Recommend actions to resolve or mitigate problems
- Provide insights on performance or resource utilization

Feel free to ask for additional context if needed, such as:
- Correlation with logs from other services
- More specific time ranges for analysis
- Queries for specific error messages or events
"""


# ==============================
# Tool Handlers
# ==============================


@mcp.tool()
@with_aws_config(CloudWatchLogsResource, method_name="get_log_groups")
async def list_log_groups(
    prefix: str = None,
    limit: int = 50,
    next_token: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    List available CloudWatch log groups with optional filtering by prefix.

    Args:
        prefix: Optional prefix to filter log groups by name
        limit: Maximum number of log groups to return (default: 50)
        next_token: Token for pagination to get the next set of results
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with log groups information
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsSearchTools)
async def search_logs(
    log_group_name: str,
    query: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Search logs using CloudWatch Logs Insights query.

    Args:
        log_group_name: The log group to search
        query: CloudWatch Logs Insights query syntax
        hours: Number of hours to look back
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with search results
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsSearchTools)
async def search_logs_multi(
    log_group_names: List[str],
    query: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Search logs across multiple log groups using CloudWatch Logs Insights.

    Args:
        log_group_names: List of log groups to search
        query: CloudWatch Logs Insights query in Logs Insights syntax
        hours: Number of hours to look back (default: 24)
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with search results
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsSearchTools)
async def filter_log_events(
    log_group_name: str,
    filter_pattern: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Filter log events by pattern across all streams in a log group.

    Args:
        log_group_name: The log group to filter
        filter_pattern: The pattern to search for (CloudWatch Logs filter syntax)
        hours: Number of hours to look back
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with filtered events
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsAnalysisTools)
async def summarize_log_activity(
    log_group_name: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Generate a summary of log activity over a specified time period.

    Args:
        log_group_name: The log group to analyze
        hours: Number of hours to look back
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with activity summary
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsAnalysisTools)
async def find_error_patterns(
    log_group_name: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Find common error patterns in logs.

    Args:
        log_group_name: The log group to analyze
        hours: Number of hours to look back
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with error patterns
    """
    # Function body is handled by the decorator
    pass


@mcp.tool()
@with_aws_config(CloudWatchLogsCorrelationTools)
async def correlate_logs(
    log_group_names: List[str],
    search_term: str,
    hours: int = 24,
    start_time: str = None,
    end_time: str = None,
    profile: str = None,
    region: str = None,
) -> str:
    """
    Correlate logs across multiple AWS services using a common search term.

    Args:
        log_group_names: List of log group names to search
        search_term: Term to search for in logs (request ID, transaction ID, etc.)
        hours: Number of hours to look back
        start_time: Optional ISO8601 start time
        end_time: Optional ISO8601 end time
        profile: Optional AWS profile name to use for credentials
        region: Optional AWS region name to use for API calls

    Returns:
        JSON string with correlated events
    """
    # Function body is handled by the decorator
    pass


def _normalize_http_path(raw_path: str) -> str:
    """Ensure HTTP route paths always start with a single leading slash.

    Git Bash/MSYS on Windows can strip the leading '/' from CLI arguments,
    which breaks Starlette route registration. This normalizer keeps the
    server robust regardless of shell behavior.
    """
    cleaned = (raw_path or "").strip()
    if not cleaned:
        return "/mcp"
    cleaned = cleaned.lstrip("/")
    return f"/{cleaned}"


def main() -> None:
    """Start the MCP server using the configured transport."""
    transport: Literal["stdio", "streamable-http", "sse"] = args.transport

    if transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = _normalize_http_path(args.streamable_http_path)
        mcp.run(transport=transport)
        return

    if transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=transport)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
