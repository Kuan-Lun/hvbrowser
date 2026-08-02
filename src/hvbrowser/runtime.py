"""Shared browser primitives used by HentaiVerse domain packages.

This module is the intentional boundary through which higher-level HV packages
access the underlying browser runtime. Keeping that boundary here avoids
requiring those packages to depend on ``hbrowser`` directly.
"""

from hbrowser.gallery.element_action import ElementAction
from hbrowser.gallery.utils import is_connection_error, setup_logger
from hbrowser.notify import notify

__all__ = [
    "ElementAction",
    "is_connection_error",
    "notify",
    "setup_logger",
]
