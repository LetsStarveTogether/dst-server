#!/usr/bin/env python3

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from enum import StrEnum
from typing import Any

from luaparser import ast

BANNED_NAMES = frozenset(("inst", "GetDebugString"))
DEFAULT_VAR = "_l"


class LuaType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    TABLE = "table"
    NIL = "nil"
    FUNCTION = "function"
    USERDATA = "userdata"
    ANY = "any"


def infer_type(node: Any) -> LuaType:
    match node:
        case ast.String() | ast.Concat():
            return LuaType.STRING
        case (
            ast.Number()
            | ast.AddOp()
            | ast.SubOp()
            | ast.MultOp()
            | ast.FloatDivOp()
            | ast.FloorDivOp()
        ):
            return LuaType.NUMBER
        case ast.TrueExpr() | ast.FalseExpr():
            return LuaType.BOOLEAN
        case ast.Table():
            return LuaType.TABLE
        case ast.Nil():
            return LuaType.NIL
        case ast.Function():
            return LuaType.FUNCTION
        case ast.Call(func=ast.Name(id="newproxy")):
            return LuaType.USERDATA
        case _:
            return LuaType.ANY


def construct_value(
    return_type: LuaType | list[LuaType] | None, value_str: str | None = None
) -> str:
    if return_type is None:
        return "variable"

    if isinstance(return_type, list):
        values = value_str.split(",") if value_str else []
        return ",".join(
            construct_value(
                return_type[i] if i < len(return_type) else None,
                values[i].strip() if i < len(values) else None,
            )
            for i in range(len(return_type))
        )

    type_map = {
        LuaType.TABLE: "{}",
        LuaType.NUMBER: "0",
        LuaType.FUNCTION: "function()end",
        LuaType.USERDATA: "newproxy()",
        LuaType.STRING: '""',
        LuaType.NIL: "nil",
        LuaType.ANY: "variable",
    }

    if return_type == LuaType.BOOLEAN:
        return (
            value_str.strip()
            if value_str and value_str.strip() in ["true", "false"]
            else "false"
        )
    elif return_type == "string?":
        return 'variable and "" or false'
    elif value_str and value_str.strip() == "inst":
        return "inst"

    return type_map.get(return_type, "variable")


class ReturnVisitor(ast.ASTVisitor):
    def __init__(self) -> None:
        self.returns: list[tuple[list[LuaType], str]] = []

    def visit_Return(self, node: Any) -> bool:
        types: list[LuaType] = [infer_type(val) for val in node.values]
        values_str: str = ""
        try:
            if node.values:
                values_str = ", ".join([ast.to_pretty_str(v) for v in node.values])
        except Exception:
            pass
        self.returns.append((types, values_str))
        return False

    def get_returns(self) -> list[tuple[list[LuaType], str]]:
        return self.returns


class BaseVisitor(ast.ASTVisitor):
    def __init__(self, filename: str, local_var: str = DEFAULT_VAR) -> None:
        self.filename = filename
        self.local_var = local_var
        self.processed = set[str]()

    def get_line_number(self, node: Any) -> str | int:
        return (
            node.first_token.line
            if hasattr(node, "first_token") and node.first_token
            else "unknown"
        )

    def build_annotations(
        self,
        params: list[str],
        return_types: list[tuple[list[LuaType], str]],
        node: Any = None,
        source_prefix: str = "",
    ) -> list[str]:
        line_number = self.get_line_number(node) if node else "1"
        annotations = [f"---@source {source_prefix}{self.filename}.lua:{line_number}"]
        for p in params:
            if p not in ["self", "..."]:
                annotations.append(f"---@param {p} any")
            elif p == "...":
                annotations.append("---@param ... any")

        if return_types:
            last_types, _ = return_types[-1]
            if last_types:
                type_strs = [str(rt) for rt in last_types]
                return_annotation = f"---@return {', '.join(type_strs)}"
                annotations.append(return_annotation)
        return annotations

    def build_function(
        self,
        name: str,
        params: list[str],
        return_types: list[tuple[list[LuaType], str]],
        node: Any = None,
        prefix: str = "",
        source_prefix: str = "",
    ) -> str:
        annotations = self.build_annotations(params, return_types, node, source_prefix)
        param_str = ", ".join(params)
        func_def = f"function {prefix}{name}({param_str})"

        if return_types:
            last_types, last_values = return_types[-1]
            constructed = construct_value(last_types, last_values)
            return "\n".join(annotations) + f"\n{func_def} return {constructed} end"
        else:
            return "\n".join(annotations) + f"\n{func_def} end"


class ComponentVisitor(BaseVisitor):
    def __init__(
        self, filename: str, class_name: str, local_var: str, folder_name: str
    ) -> None:
        super().__init__(filename, local_var)
        self.class_name = class_name
        self.folder_name = folder_name
        self.methods: list[str] = []
        self.class_vars: dict[str, tuple[LuaType | None, str | None]] = {}

    def visit_Method(self, node: Any) -> bool | None:
        if (
            isinstance(node.source, ast.Name)
            and node.source.id == self.class_name
            and isinstance(node.name, ast.Name)
        ):
            method_name = node.name.id
            if method_name in BANNED_NAMES or method_name in self.processed:
                return

            self.processed.add(method_name)
            params = []
            for p in node.args:
                if hasattr(p, "id"):
                    params.append(p.id)
                elif hasattr(p, "__class__") and p.__class__.__name__ == "Varargs":
                    params.append("...")
                else:
                    params.append("arg")

            return_visitor = ReturnVisitor()
            return_visitor.visit(node.body)
            return_types = return_visitor.get_returns()

            func_def = self.build_function(
                f"{self.local_var}:{method_name}",
                params,
                return_types,
                node,
                source_prefix=f"{self.folder_name}/",
            )
            self.methods.append(func_def)
        return False

    def visit_Assign(self, node: Any) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Index)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(target.idx, ast.Name)
            ):
                var_name = target.idx.id
                if var_name in BANNED_NAMES or var_name in self.processed:
                    continue

                var_type, var_value_str = None, None
                if node.values:
                    value_node = node.values[0]
                    var_type = infer_type(value_node)
                    try:
                        var_value_str = (
                            "function() end"
                            if isinstance(
                                value_node,
                                (ast.Function, ast.Method, ast.AnonymousFunction),
                            )
                            else ast.to_pretty_str(value_node)
                        )
                    except Exception:
                        var_value_str = "variable"

                if var_name not in self.class_vars or self.class_vars[var_name][0] in [
                    None,
                    LuaType.ANY,
                ]:
                    self.class_vars[var_name] = (var_type, var_value_str)

            elif (
                isinstance(target, ast.Index)
                and isinstance(target.value, ast.Name)
                and target.value.id == self.class_name
                and isinstance(target.idx, ast.Name)
                and node.values
                and isinstance(node.values[0], ast.AnonymousFunction)
            ):
                func_name = target.idx.id
                if func_name in BANNED_NAMES or func_name in self.processed:
                    continue

                self.processed.add(func_name)
                func_node = node.values[0]
                params = []
                for p in func_node.args:
                    if hasattr(p, "id"):
                        params.append(p.id)
                    elif hasattr(p, "__class__") and p.__class__.__name__ == "Varargs":
                        params.append("...")
                    else:
                        params.append("arg")

                return_visitor = ReturnVisitor()
                return_visitor.visit(func_node.body)
                return_types = return_visitor.get_returns()

                func_def = self.build_function(
                    func_name,
                    params,
                    return_types,
                    func_node,
                    source_prefix=f"{self.folder_name}/",
                )
                self.methods.append(func_def)

    def get_definitions(self) -> tuple[list[str], list[str]]:
        sorted_vars = sorted(self.class_vars.items(), key=lambda x: x[0])
        field_annotations = [
            f"---@field {name} {var_type or 'any'}"
            for name, (var_type, _) in sorted_vars
        ]
        var_defs = [
            f"{self.local_var}.{name}={construct_value(var_type, var_value_str)}"
            for name, (var_type, var_value_str) in sorted_vars
        ]
        return field_annotations, self.methods + var_defs


class ModutilVisitor(BaseVisitor):
    def __init__(self, filename: str, local_var: str = "_m") -> None:
        super().__init__(filename, local_var)
        self.functions: list[str] = []

    def visit_Assign(self, node: Any) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Index)
                and isinstance(target.value, ast.Name)
                and target.value.id == "env"
                and isinstance(target.idx, ast.Name)
                and node.values
                and isinstance(node.values[0], ast.AnonymousFunction)
            ):
                func_name = target.idx.id
                if func_name in self.processed:
                    continue

                self.processed.add(func_name)
                func_node = node.values[0]
                params = []
                for p in func_node.args:
                    if hasattr(p, "id"):
                        params.append(p.id)
                    elif hasattr(p, "__class__") and p.__class__.__name__ == "Varargs":
                        params.append("...")
                    else:
                        params.append("arg")

                return_visitor = ReturnVisitor()
                return_visitor.visit(func_node.body)
                return_types = return_visitor.get_returns()

                func_def = self.build_function(
                    func_name, params, return_types, func_node
                )
                self.functions.append(func_def)

    def get_definitions(self) -> list[str]:
        return self.functions


def parse_component(
    content: str, filename: str, class_name: str, local_var: str, folder_name: str
) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(content)
        visitor = ComponentVisitor(filename, class_name, local_var, folder_name)
        visitor.visit(tree)
        return visitor.get_definitions()
    except Exception as e:
        print(f"Error parsing {filename}.lua: {e}")
        return [], []


def parse_modutil(content: str, filename: str) -> list[str]:
    try:
        tree = ast.parse(content)
        visitor = ModutilVisitor(filename)
        visitor.visit(tree)
        return visitor.get_definitions()
    except Exception as e:
        print(f"Error parsing {filename}.lua: {e}")
        return []


def extract_class_name(lines: list[str]) -> str | None:
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("return "):
            parts = line.split(" ")
            if len(parts) > 1:
                return parts[1].replace(",", "")
    return None


def process_component_file(file_path: str, folder_name: str) -> tuple[str, str] | None:
    try:
        filename_no_ext = os.path.splitext(os.path.basename(file_path))[0]
        local_var = DEFAULT_VAR

        with open(file_path, "r", encoding="utf-8") as f:
            content_lines = f.readlines()
            content = "".join(content_lines)

            class_name = extract_class_name(content_lines) or filename_no_ext
            field_annotations, defs = parse_component(
                content, filename_no_ext, class_name, local_var, folder_name
            )

            result_lines = [f"---@class {class_name}"]
            result_lines.extend(field_annotations)
            result_lines.extend(
                [
                    f"local {local_var}={{}}",
                    f"{folder_name}.{filename_no_ext}={local_var}",
                    "",
                ]
            )
            result_lines.extend(defs)
            result_lines.append("")

            return filename_no_ext, "\n".join(result_lines)
    except Exception as e:
        print(f"Error processing file {file_path}: {e}")
        return None


def process_modutil_file(file_path: str) -> str | None:
    try:
        filename_no_ext = os.path.splitext(os.path.basename(file_path))[0]

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            defs = parse_modutil(content, filename_no_ext)
            return "\n".join(defs) + "\n" if defs else None
    except Exception as e:
        print(f"Error processing modutil file {file_path}: {e}")
        return None


def detect_file_type(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            return (
                "modutil"
                if "env." in content and "= function(" in content
                else "component"
            )
    except Exception:
        return "component"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate LSP-compatible Lua annotations"
    )
    parser.add_argument("input", help="Input directory (components) or file (modutil)")
    parser.add_argument("-o", "--output_file", help="Output file name")
    parser.add_argument("-m", "--max-workers", type=int, help="Max worker processes")
    parser.add_argument(
        "--mode", choices=["components", "modutil", "auto"], default="auto"
    )

    args = parser.parse_args()
    input_path = args.input
    mode = args.mode

    if mode == "auto":
        if (
            os.path.isfile(input_path)
            and "modutil" in os.path.basename(input_path).lower()
        ):
            mode = "modutil"
        elif os.path.isdir(input_path):
            mode = "components"
        else:
            print("Cannot auto-detect mode. Please specify --mode explicitly.")
            sys.exit(1)

    if mode == "modutil":
        process_modutil_mode(args, input_path)
    elif mode == "components":
        process_components_mode(args, input_path)


def process_modutil_mode(args: argparse.Namespace, input_path: str) -> None:
    if not os.path.isfile(input_path):
        print(f"Error: Modutil file not found at {input_path}")
        sys.exit(1)

    filename_no_ext = os.path.splitext(os.path.basename(input_path))[0]
    output_file = args.output_file or f"{filename_no_ext}_def.lua"

    print(f"Processing modutil file: {input_path}")
    result = process_modutil_file(input_path)

    if result:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Output written to {output_file}")
    else:
        print("No definitions found in modutil file")


def process_components_mode(args: argparse.Namespace, input_path: str) -> None:
    if not os.path.isdir(input_path):
        print(f"Error: Input directory not found at {input_path}")
        sys.exit(1)

    folder_name = os.path.basename(os.path.normpath(input_path))
    output_file = args.output_file or f"{folder_name}_def.lua"
    max_workers = args.max_workers

    lua_files = []
    for root, _, files in os.walk(input_path):
        for file in files:
            if file.endswith(".lua"):
                lua_files.append(os.path.join(root, file))

    if not lua_files:
        print("No Lua files found in the specified directory")
        return

    print(f"Processing {len(lua_files)} files with {max_workers or 'auto'} workers")

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(process_component_file, file_path, folder_name): file_path
            for file_path in lua_files
        }

        results = []
        completed = 0

        for future in as_completed(future_to_file):
            result = future.result()
            results.append(result)
            completed += 1

            if completed % 10 == 0 or completed == len(lua_files):
                print(
                    f"Progress: {completed}/{len(lua_files)} files processed "
                    f"({completed / len(lua_files) * 100:.1f}%)"
                )

    successful_results = [r for r in results if r is not None]
    successful_results.sort(key=lambda x: x[0])

    print(f"Successfully processed {len(successful_results)}/{len(lua_files)} files")

    with open(output_file, "w", encoding="utf-8") as f:
        for _, content in successful_results:
            f.write(content)
            f.write("\n")

    print(f"Output written to {output_file}")


if __name__ == "__main__":
    main()
