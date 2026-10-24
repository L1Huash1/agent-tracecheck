"""Offline checks for normalized agent tool-call traces."""

from .core import TraceError, analyze_trace

__version__ = "0.2.0"
__all__ = ["TraceError", "analyze_trace", "__version__"]
