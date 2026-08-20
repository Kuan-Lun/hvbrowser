import unittest

from hbrowser.gallery.utils import (
    ZendriverOperationTimeout as HbrowserZendriverOperationTimeout,
)
from hbrowser.gallery.utils import (
    is_browser_generation_error as hbrowser_is_browser_generation_error,
)
from hbrowser.gallery.utils import wait_for_zendriver as hbrowser_wait_for_zendriver

import hvbrowser.runtime as runtime
from hvbrowser.runtime import (
    ZendriverOperationTimeout,
    is_browser_generation_error,
    wait_for_zendriver,
)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_reexports_zendriver_timeout_primitives(self) -> None:
        self.assertIs(ZendriverOperationTimeout, HbrowserZendriverOperationTimeout)
        self.assertIs(
            is_browser_generation_error,
            hbrowser_is_browser_generation_error,
        )
        self.assertIs(wait_for_zendriver, hbrowser_wait_for_zendriver)

    def test_legacy_connection_classifier_is_removed(self) -> None:
        self.assertFalse(hasattr(runtime, "is_connection_error"))


if __name__ == "__main__":
    unittest.main()
