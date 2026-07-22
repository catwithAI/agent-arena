"""MCP intermediate representation and profile dialects."""

from .command_register import CommandRegisterDialect
from .base import McpDialectError, ResolvedMcpServer, resolve_mcp_servers
from .json_file import JsonFileDialect, McpRenderResult
from .native_config import NativeConfigDialect
from .spi import McpCommandResult, McpLifecycleDialect, McpPrepared

__all__ = [
    "CommandRegisterDialect",
    "JsonFileDialect",
    "McpDialectError",
    "McpCommandResult",
    "McpLifecycleDialect",
    "McpPrepared",
    "McpRenderResult",
    "NativeConfigDialect",
    "ResolvedMcpServer",
    "resolve_mcp_servers",
]
