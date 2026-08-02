import ast
import unittest
from pathlib import Path

from hvbrowser import (
    HENTAIVERSE_ISEKAI_ROOT_URL,
    HENTAIVERSE_ROOT_URL,
    HVDriver,
)


class ArchitectureTests(unittest.TestCase):
    def test_hvbrowser_does_not_import_hvbattle(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        imported_modules: set[str] = set()

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)

        self.assertFalse(
            any(
                name == "hvbattle" or name.startswith("hvbattle.")
                for name in imported_modules
            )
        )

    def test_battle_modules_are_not_part_of_hvbrowser(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"

        self.assertEqual(list(source_root.glob("hv_battle*.py")), [])

    def test_hvbrowser_owns_and_installs_hentaiverse_urls(self) -> None:
        driver = HVDriver(headless=True)

        self.assertEqual(driver.url["HentaiVerse"], HENTAIVERSE_ROOT_URL)
        self.assertEqual(driver.url["HentaiVerse isekai"], HENTAIVERSE_ISEKAI_ROOT_URL)


if __name__ == "__main__":
    unittest.main()
