import ast
import unittest
from pathlib import Path

from hvbrowser import (
    HENTAIVERSE_ISEKAI_ROOT_URL,
    HENTAIVERSE_ROOT_URL,
    HentaiVerseSession,
)

_ZENDRIVER_PROTOCOL_METHODS = {
    "apply",
    "clear_input",
    "click",
    "evaluate",
    "focus",
    "get_content",
    "get_position",
    "mouse_click",
    "mouse_move",
    "query_selector",
    "query_selector_all",
    "scroll",
    "select",
    "send",
    "send_keys",
    "xpath",
}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _attribute_parts(node: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _is_driver_receiver(node: ast.expr) -> bool:
    parts = _attribute_parts(node)
    return bool(parts and parts[-1] in {"driver", "_driver"})


def _is_zendriver_protocol_call(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    method = call.func.attr
    if method in _ZENDRIVER_PROTOCOL_METHODS:
        return True
    parts = _attribute_parts(call.func.value)
    if method in {"get", "wait"}:
        return bool(
            parts
            and parts[-1] in {"browser", "connection", "page", "tab"}
            and not _is_driver_receiver(call.func.value)
        )
    return False


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name), None
    )


def _constant_numbers(tree: ast.AST) -> dict[str, float]:
    values: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int | float)
            and not isinstance(node.value.value, bool)
        ):
            values[target.id] = float(node.value.value)
    return values


def _number(node: ast.expr | None, constants: dict[str, float]) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


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

    def test_hbrowser_runtime_primitives_only_cross_the_runtime_module(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        violations: list[str] = []

        for source_file in source_root.glob("*.py"):
            if source_file.name == "runtime.py":
                continue
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "hbrowser.gallery.utils"
                ) or (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name == "hbrowser.gallery.utils" for alias in node.names
                    )
                ):
                    violations.append(f"{source_file.name}:{node.lineno}")

        self.assertEqual(violations, [])

    def test_hvbrowser_owns_and_installs_hentaiverse_urls(self) -> None:
        session = HentaiVerseSession(headless=True)

        self.assertEqual(
            session.browser.url["HentaiVerse"],
            HENTAIVERSE_ROOT_URL,
        )
        self.assertEqual(
            session.browser.url["HentaiVerse isekai"],
            HENTAIVERSE_ISEKAI_ROOT_URL,
        )

    def test_production_zendriver_calls_use_strict_owned_watchdogs(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        violations: list[str] = []

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            constants = _constant_numbers(tree)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                if _call_name(node) == "wait_for_zendriver":
                    owner = _keyword(node, "owner")
                    timeout = _keyword(node, "timeout")
                    if not isinstance(parents.get(node), ast.Await):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog is not awaited"
                        )
                    if (
                        owner is None
                        or isinstance(owner, ast.Constant)
                        and owner.value is None
                    ):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog owner is missing"
                        )
                    if timeout is None:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog timeout is missing"
                        )
                    elif (resolved_timeout := _number(timeout, constants)) is None or (
                        resolved_timeout <= 0
                    ):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog timeout must "
                            "be an explicit positive budget"
                        )
                    if not node.args:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog awaitable is missing"
                        )
                        continue

                    operation = node.args[0]
                    if (
                        isinstance(operation, ast.Call)
                        and _is_zendriver_protocol_call(operation)
                        and isinstance(operation.func, ast.Attribute)
                        and owner is not None
                        and ast.dump(operation.func.value) != ast.dump(owner)
                    ):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: watchdog owner is not "
                            "the exact protocol receiver"
                        )

                    if (
                        isinstance(operation, ast.Call)
                        and isinstance(operation.func, ast.Attribute)
                        and operation.func.attr in {"select", "xpath"}
                    ):
                        inner = _number(_keyword(operation, "timeout"), constants)
                        outer = _number(timeout, constants)
                        if inner is None or outer is None or outer - inner < 2:
                            violations.append(
                                f"{source_file.name}:{node.lineno}: selector watchdog "
                                "must leave at least two seconds of outer margin"
                            )
                    continue

                if _is_zendriver_protocol_call(node):
                    parent = parents.get(node)
                    safely_wrapped = (
                        isinstance(parent, ast.Call)
                        and _call_name(parent) == "wait_for_zendriver"
                        and bool(parent.args)
                        and parent.args[0] is node
                    )
                    if not safely_wrapped:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: raw Zendriver "
                            f"{node.func.attr} call"
                        )

                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "wait"
                    and _is_driver_receiver(node.func.value)
                ):
                    if not isinstance(parents.get(node), ast.Await):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: Driver.wait is not awaited"
                        )
                    owner = _keyword(node, "owner")
                    operation_timeout = _keyword(node, "operation_timeout")
                    action = node.args[0] if node.args else None
                    action_owner = (
                        action.value if isinstance(action, ast.Attribute) else None
                    )
                    if owner is None or operation_timeout is None:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: Driver.wait requires "
                            "owner and operation_timeout"
                        )
                    elif (
                        resolved_operation_timeout := _number(
                            operation_timeout, constants
                        )
                    ) is None or resolved_operation_timeout <= 0:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: Driver.wait operation "
                            "timeout must be an explicit positive budget"
                        )
                    elif action_owner is not None and ast.dump(
                        action_owner
                    ) != ast.dump(owner):
                        violations.append(
                            f"{source_file.name}:{node.lineno}: Driver.wait owner is not "
                            "the exact action receiver"
                        )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
