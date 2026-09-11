from typing import Any

from luaparser import ast

from .values import LuaType, infer_type

type ReturnInfo = list[LuaType]

BANNED_NAMES = ("inst", "GetDebugString")
DEFAULT_VAR = "_l"
FUNCTION_NODES = (ast.Function, ast.AnonymousFunction, ast.LocalFunction, ast.Method)


def argument_names(arguments: Any) -> list[str]:
    names = []
    for argument in arguments:
        name = getattr(argument, "id", None)
        if isinstance(name, str):
            names.append(name)
        elif type(argument).__name__ == "Varargs":
            names.append("...")
        else:
            names.append("arg")
    return names


def indexed_name(node: Any, owner: str) -> str | None:
    if not isinstance(node, ast.Index):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id != owner:
        return None
    return node.idx.id if isinstance(node.idx, ast.Name) else None


def method_name(node: Any, owner: str) -> str | None:
    if not isinstance(node.source, ast.Name) or node.source.id != owner:
        return None
    return node.name.id if isinstance(node.name, ast.Name) else None


def anonymous_function(node: Any) -> Any | None:
    values = getattr(node, "values", ())
    if not values or not isinstance(values[0], ast.AnonymousFunction):
        return None
    return values[0]


class ReturnVisitor(ast.ASTRecursiveVisitor):
    def __init__(self) -> None:
        self.returns: list[ReturnInfo] = []
        self.function_depth = 0

    def enter_Expression(self, node: ast.Expression) -> None:
        if isinstance(node, FUNCTION_NODES):
            self.function_depth += 1

    def exit_Expression(self, node: ast.Expression) -> None:
        if isinstance(node, FUNCTION_NODES):
            self.function_depth -= 1

    def enter_Return(self, node: Any) -> None:
        if self.function_depth:
            return
        values = getattr(node, "values", ())
        self.returns.append([infer_type(value) for value in values])


def return_info(body: Any) -> list[ReturnInfo]:
    visitor = ReturnVisitor()
    visitor.visit(body)
    return visitor.returns


class BaseVisitor(ast.ASTVisitor):
    def __init__(self, filename: str, local_var: str) -> None:
        self.filename = filename
        self.local_var = local_var
        self.processed = set[str]()

    def build_annotations(
        self,
        parameters: list[str],
        returns: list[ReturnInfo],
        node: Any,
        source_prefix: str = "",
    ) -> list[str]:
        token = getattr(node, "first_token", None)
        line = getattr(token, "line", "unknown")
        annotations = [f"---@source {source_prefix}{self.filename}.lua:{line}"]

        for parameter in parameters:
            if parameter == "...":
                annotations.append("---@param ... any")
            elif parameter != "self":
                annotations.append(f"---@param {parameter} any")

        if returns and returns[-1]:
            annotations.append(f"---@return {', '.join(returns[-1])}")

        return annotations

    def build_function(
        self,
        name: str,
        parameters: list[str],
        returns: list[ReturnInfo],
        node: Any,
        source_prefix: str = "",
    ) -> str:
        lines = self.build_annotations(parameters, returns, node, source_prefix)
        lines.append(f"function {name}({', '.join(parameters)}) end")
        return "\n".join(lines)


class ComponentVisitor(BaseVisitor):
    def __init__(
        self,
        filename: str,
        class_name: str,
        folder_name: str,
        local_var: str = DEFAULT_VAR,
    ) -> None:
        super().__init__(filename, local_var)
        self.class_name = class_name
        self.folder_name = folder_name
        self.methods: list[str] = []
        self.fields: dict[str, LuaType] = {}

    def visit_Method(self, node: Any) -> bool:
        name = method_name(node, self.class_name)
        if name is None or name in BANNED_NAMES or name in self.processed:
            return False

        self.processed.add(name)
        self.methods.append(
            self.build_function(
                f"{self.local_var}:{name}",
                argument_names(node.args),
                return_info(node.body),
                node,
                source_prefix=f"{self.folder_name}/",
            )
        )
        return False

    def visit_Assign(self, node: Any) -> None:
        function = anonymous_function(node)
        for target in node.targets:
            if name := indexed_name(target, "self"):
                self.record_field(name, node)
                continue

            if function is not None and (name := indexed_name(target, self.class_name)):
                self.record_function(name, function)

    def record_field(self, name: str, node: Any) -> None:
        if name in BANNED_NAMES or name in self.processed:
            return

        if self.fields.get(name) in {None, LuaType.ANY}:
            self.fields[name] = (
                infer_type(node.values[0]) if node.values else LuaType.ANY
            )

    def record_function(self, name: str, node: Any) -> None:
        if name in BANNED_NAMES or name in self.processed:
            return

        self.processed.add(name)
        self.methods.append(
            self.build_function(
                f"{self.local_var}.{name}",
                argument_names(node.args),
                return_info(node.body),
                node,
                source_prefix=f"{self.folder_name}/",
            )
        )

    def definitions(self) -> tuple[list[str], list[str]]:
        annotations = [
            f"---@field {name} {value_type}"
            for name, value_type in sorted(self.fields.items())
        ]
        return annotations, self.methods


class ModutilVisitor(BaseVisitor):
    def __init__(self, filename: str) -> None:
        super().__init__(filename, "_m")
        self.functions: list[str] = []

    def visit_Assign(self, node: Any) -> None:
        function = anonymous_function(node)
        if function is None:
            return

        for target in node.targets:
            name = indexed_name(target, "env")
            if name is None or name in self.processed:
                continue

            self.processed.add(name)
            self.functions.append(
                self.build_function(
                    name,
                    argument_names(function.args),
                    return_info(function.body),
                    function,
                )
            )


def parse_component(
    content: str,
    filename: str,
    class_name: str,
    folder_name: str,
) -> tuple[list[str], list[str]]:
    visitor = ComponentVisitor(filename, class_name, folder_name)
    visitor.visit(ast.parse(content))
    return visitor.definitions()


def parse_modutil(content: str, filename: str) -> list[str]:
    visitor = ModutilVisitor(filename)
    visitor.visit(ast.parse(content))
    return visitor.functions
