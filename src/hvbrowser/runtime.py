"""Shared browser primitives used by HentaiVerse domain packages.

This module is the intentional boundary through which higher-level HV packages
access the underlying browser runtime. Keeping that boundary here avoids
requiring those packages to depend on ``hbrowser`` directly.
"""

from hbrowser.gallery.element_action import ElementAction
from hbrowser.gallery.utils import (
    ZendriverOperationTimeout,
    is_browser_generation_error,
    log_context,
    setup_logger,
    wait_for_zendriver,
)
from hbrowser.notify import notify

__all__ = [
    "ElementAction",
    "ZendriverOperationTimeout",
    "is_browser_generation_error",
    "log_context",
    "notify",
    "setup_logger",
    "wait_for_zendriver",
]
