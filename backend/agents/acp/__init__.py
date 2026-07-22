"""ACP v1 transport and registry support.

ACP agents share this protocol implementation; registry entries are data and
never cause an adapter module to be imported or a package to be installed.
"""

from .client import AcpClient, AcpClientError, AcpRunResult
from .parser import AcpParser
from .registry import AcpRegistryResolver, ResolvedAcpAgent

__all__ = [
    "AcpClient",
    "AcpClientError",
    "AcpParser",
    "AcpRegistryResolver",
    "AcpRunResult",
    "ResolvedAcpAgent",
]
