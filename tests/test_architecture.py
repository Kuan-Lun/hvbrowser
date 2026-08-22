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
    def test_server_mutations_use_a_receipt_budget_separate_from_command_cap(
        self,
    ) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        for filename in (
            "equipment_repair.py",
            "lottery.py",
            "market.py",
            "monster_lab.py",
            "player.py",
        ):
            source = (source_root / filename).read_text()
            self.assertIn("SERVER_STATE_RECEIPT_TIMEOUT_SECONDS", source, filename)

        runtime_constants = _constant_numbers(
            ast.parse((source_root / "runtime.py").read_text())
        )
        self.assertLessEqual(
            runtime_constants["PROTOCOL_COMMAND_TIMEOUT_SECONDS"],
            5,
        )
        self.assertGreater(
            runtime_constants["SERVER_STATE_RECEIPT_TIMEOUT_SECONDS"],
            6,
        )

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

    def test_maintenance_clients_have_no_menu_navigation_path(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        client_files = (
            "equipment_repair.py",
            "lottery.py",
            "monster_lab.py",
        )
        forbidden_calls = {"get_position", "mouse_move", "select_bazaar"}
        violations: list[str] = []

        for filename in client_files:
            source_file = source_root / filename
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            direct_get_count = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and node.name.endswith(
                    "_from_menu"
                ):
                    violations.append(f"{filename}:{node.lineno}: menu helper")
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                if node.func.attr in forbidden_calls:
                    violations.append(
                        f"{filename}:{node.lineno}: {node.func.attr} navigation"
                    )
                if node.func.attr == "get" and _is_driver_receiver(node.func.value):
                    direct_get_count += 1
            if direct_get_count != 1:
                violations.append(
                    f"{filename}: expected one canonical Driver.get path, "
                    f"found {direct_get_count}"
                )

        self.assertEqual(violations, [])

    def test_dead_maintenance_menu_api_is_absent(self) -> None:
        repository = Path(__file__).parents[1]
        source_root = repository / "src" / "hvbrowser"
        maintenance_tree = ast.parse(
            (source_root / "maintenance_navigation.py").read_text()
        )
        forbidden_definitions = {
            node.name
            for node in ast.walk(maintenance_tree)
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name
            in {
                "MaintenanceNavigator",
                "classify_maintenance_navigation_blocker",
            }
        }
        self.assertEqual(forbidden_definitions, set())

        session_source = (source_root / "session.py").read_text()
        package_source = (source_root / "__init__.py").read_text()
        self.assertNotIn("maintenance_navigation =", session_source)
        self.assertNotIn('"MaintenanceNavigator"', package_source)
        self.assertNotIn('"classify_maintenance_navigation_blocker"', package_source)

        constructor_parameters = {
            "equipment_repair.py": {"driver", "realm"},
            "lottery.py": {"driver"},
            "monster_lab.py": {"driver"},
        }
        for filename, expected in constructor_parameters.items():
            tree = ast.parse((source_root / filename).read_text())
            positional = {
                argument.arg
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "__init__"
                for argument in node.args.args
                if argument.arg != "self"
            }
            self.assertEqual(positional, expected, filename)

    def test_persistent_maintenance_context_is_required_by_public_api(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "hvbrowser"
        required_methods = {
            "lottery.py": {"inspect", "purchase"},
            "monster_lab.py": {"inspect", "feed_all"},
        }
        violations: list[str] = []
        for filename, method_names in required_methods.items():
            tree = ast.parse((source_root / filename).read_text())
            methods = {
                node.name: node
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name in method_names
            }
            for method_name in method_names:
                method = methods.get(method_name)
                if method is None:
                    violations.append(f"{filename}:{method_name}: missing")
                    continue
                context_indexes = [
                    index
                    for index, argument in enumerate(method.args.kwonlyargs)
                    if argument.arg == "context"
                ]
                if len(context_indexes) != 1:
                    violations.append(
                        f"{filename}:{method_name}: context is not keyword-only"
                    )
                    continue
                if method.args.kw_defaults[context_indexes[0]] is not None:
                    violations.append(
                        f"{filename}:{method_name}: context has a default"
                    )

        self.assertEqual(violations, [])

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
        protocol_cap = 5.0

        for source_file in source_root.glob("*.py"):
            tree = ast.parse(source_file.read_text(), filename=str(source_file))
            parents = {
                child: parent
                for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)
            }
            constants = _constant_numbers(tree)

            for name, value in constants.items():
                if (
                    "TIMEOUT" in name
                    and name
                    not in {
                        "_ACCOUNT_CLOSE_TIMEOUT_SECONDS",
                        "SERVER_STATE_RECEIPT_TIMEOUT_SECONDS",
                    }
                    and value > protocol_cap
                ):
                    violations.append(
                        f"{source_file.name}: {name} exceeds the protocol cap"
                    )

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                if _call_name(node) in {
                    "wait_for_zendriver",
                    "wait_for_zendriver_mutation",
                }:
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
                    else:
                        resolved_timeout = _number(timeout, constants)
                        if resolved_timeout is not None and not (
                            0 < resolved_timeout <= protocol_cap
                        ):
                            violations.append(
                                f"{source_file.name}:{node.lineno}: watchdog timeout "
                                "exceeds the five-second command cap"
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

                    continue

                if _is_zendriver_protocol_call(node):
                    ancestor = parents.get(node)
                    safely_wrapped = False
                    while ancestor is not None:
                        if isinstance(ancestor, ast.Call) and _call_name(ancestor) in {
                            "wait_for_zendriver",
                            "wait_for_zendriver_mutation",
                            "evaluate_page",
                            "query_page",
                            "invoke_mutation",
                        }:
                            safely_wrapped = True
                            break
                        if isinstance(
                            ancestor,
                            ast.FunctionDef | ast.AsyncFunctionDef,
                        ):
                            break
                        ancestor = parents.get(ancestor)
                    if not safely_wrapped:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: raw Zendriver "
                            f"{node.func.attr} call"
                        )

                if isinstance(node.func, ast.Attribute) and (
                    node.func.attr in {"select", "xpath"}
                    or node.func.attr == "wait"
                    and _attribute_parts(node.func.value)[-1:]
                    in {("page",), ("driver",), ("_driver",)}
                ):
                    violations.append(
                        f"{source_file.name}:{node.lineno}: polling/fixed wait "
                        f"{node.func.attr} is forbidden"
                    )

                if _call_name(node) == "sleep" and node.args:
                    duration = _number(node.args[0], constants)
                    if duration is not None and duration > 0:
                        violations.append(
                            f"{source_file.name}:{node.lineno}: fixed readiness sleep"
                        )

                if _keyword(node, "await_promise") is not None:
                    violations.append(
                        f"{source_file.name}:{node.lineno}: page-side promise wait"
                    )

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
