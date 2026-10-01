//! Generate Lua language-server declarations from native game scripts.

use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use full_moon::{
    LuaVersion,
    ast::{self, BinOp, Expression, FunctionBody, Index, Parameter, Prefix, Suffix, Var},
    node::Node,
    tokenizer::{Symbol, TokenReference, TokenType},
    visitors::{Visit, Visitor},
};

fn parse(content: &str) -> Result<ast::Ast> {
    ensure!(
        content.len() <= crate::lua::MAX_SOURCE_BYTES,
        "Lua source exceeds the byte limit"
    );
    let source = crate::lua::normalize_line_endings(content);
    let ast = full_moon::parse_fallible(&source, LuaVersion::lua51())
        .into_result()
        .map_err(|errors| anyhow::anyhow!("invalid Lua syntax: {errors:?}"))?;
    ensure!(
        ast.to_string() == source,
        "Lua parser did not consume the complete source"
    );
    Ok(ast)
}

fn identifier(token: &TokenReference) -> &str {
    match token.token_type() {
        TokenType::Identifier { identifier } => identifier.as_str(),
        _ => "arg",
    }
}

fn unparenthesized(mut value: &Expression) -> &Expression {
    while let Expression::Parentheses { expression, .. } = value {
        value = expression;
    }
    value
}

fn prefix_name(prefix: &Prefix) -> Option<&str> {
    match prefix {
        Prefix::Name(name) => Some(identifier(name)),
        Prefix::Expression(expression) => match unparenthesized(expression) {
            Expression::Var(Var::Name(name)) => Some(identifier(name)),
            _ => None,
        },
        _ => None,
    }
}

fn infer_type(value: &Expression) -> &'static str {
    match unparenthesized(value) {
        Expression::String(_)
        | Expression::BinaryOperator {
            binop: BinOp::TwoDots(_),
            ..
        } => "string",
        Expression::Number(_)
        | Expression::BinaryOperator {
            binop: BinOp::Plus(_) | BinOp::Minus(_) | BinOp::Star(_) | BinOp::Slash(_),
            ..
        } => "number",
        Expression::TableConstructor(_) => "table",
        Expression::Symbol(token) => match token.token_type() {
            TokenType::Symbol {
                symbol: Symbol::True | Symbol::False,
            } => "boolean",
            TokenType::Symbol {
                symbol: Symbol::Nil,
            } => "nil",
            _ => "any",
        },
        Expression::FunctionCall(call)
            if prefix_name(call.prefix()) == Some("newproxy")
                && matches!(
                    call.suffixes().next(),
                    Some(Suffix::Call(ast::Call::AnonymousCall(_)))
                )
                && call.suffixes().count() == 1 =>
        {
            "userdata"
        }
        _ => "any",
    }
}

#[derive(Default)]
struct Returns {
    depth: usize,
    last: Vec<&'static str>,
}

impl Visitor for Returns {
    fn visit_function_body(&mut self, _: &FunctionBody) {
        self.depth += 1;
    }
    fn visit_function_body_end(&mut self, _: &FunctionBody) {
        self.depth -= 1;
    }
    fn visit_return(&mut self, node: &ast::Return) {
        if self.depth == 0 {
            self.last = node.returns().iter().map(infer_type).collect();
        }
    }
}

fn definition(name: &str, body: &FunctionBody, source: &str, line: usize) -> String {
    let parameters: Vec<_> = body
        .parameters()
        .iter()
        .map(|parameter| match parameter {
            Parameter::Name(name) => identifier(name),
            Parameter::Ellipsis(_) => "...",
            _ => "arg",
        })
        .collect();
    let mut lines = vec![format!("---@source {source}.lua:{line}")];
    lines.extend(
        parameters
            .iter()
            .filter(|name| **name != "self")
            .map(|name| format!("---@param {name} any")),
    );
    let mut returns = Returns::default();
    body.block().visit(&mut returns);
    if !returns.last.is_empty() {
        lines.push(format!("---@return {}", returns.last.join(", ")));
    }
    lines.push(format!("function {name}({}) end", parameters.join(", ")));
    lines.join("\n")
}

fn indexed_name<'a>(variable: &'a Var, owner: &str) -> Option<&'a str> {
    let Var::Expression(value) = variable else {
        return None;
    };
    if prefix_name(value.prefix()) != Some(owner) {
        return None;
    }
    let mut suffixes = value.suffixes();
    let name = match suffixes.next()? {
        Suffix::Index(Index::Dot { name, .. }) => identifier(name),
        Suffix::Index(Index::Brackets { expression, .. }) => match unparenthesized(expression) {
            Expression::Var(Var::Name(name)) => identifier(name),
            _ => return None,
        },
        _ => return None,
    };
    suffixes.next().is_none().then_some(name)
}

struct Definitions<'a> {
    owner: &'a str,
    source: String,
    component: bool,
    processed: HashSet<String>,
    fields: BTreeMap<String, &'static str>,
    functions: Vec<String>,
    skipped_if: Option<usize>,
}

impl<'a> Definitions<'a> {
    fn new(owner: &'a str, source: String, component: bool) -> Self {
        Self {
            owner,
            source,
            component,
            processed: HashSet::new(),
            fields: BTreeMap::new(),
            functions: Vec::new(),
            skipped_if: None,
        }
    }

    fn banned(&self, name: &str) -> bool {
        self.component && matches!(name, "inst" | "GetDebugString")
    }

    fn function(&mut self, name: &str, body: &FunctionBody, line: usize, method: bool) {
        if self.banned(name) || !self.processed.insert(name.into()) {
            return;
        }
        let name = if self.component {
            format!("_l{}{name}", if method { ":" } else { "." })
        } else {
            name.into()
        };
        self.functions
            .push(definition(&name, body, &self.source, line));
    }
}

impl Visitor for Definitions<'_> {
    fn visit_assignment(&mut self, assignment: &ast::Assignment) {
        if self.skipped_if.is_some() {
            return;
        }
        let Some(value) = assignment.expressions().iter().next() else {
            return;
        };
        for target in assignment.variables() {
            if self.component
                && let Some(name) = indexed_name(target, "self")
            {
                if !self.banned(name)
                    && !self.processed.contains(name)
                    && matches!(self.fields.get(name), None | Some(&"any"))
                {
                    self.fields.insert(name.into(), infer_type(value));
                }
            } else if let Expression::Function(function) = unparenthesized(value)
                && let Some(name) = indexed_name(target, self.owner)
            {
                self.function(
                    name,
                    function.body(),
                    function.function_token().token().start_position().line(),
                    false,
                );
            }
        }
    }

    fn visit_function_declaration(&mut self, function: &ast::FunctionDeclaration) {
        if self.skipped_if.is_some() || !self.component {
            return;
        }
        let names = function.name();
        if names.names().len() == 1
            && identifier(names.names().iter().next().unwrap()) == self.owner
            && let Some(name) = names.method_name()
        {
            self.function(
                identifier(name),
                function.body(),
                function.function_token().token().start_position().line(),
                true,
            );
        }
    }

    fn visit_if(&mut self, node: &ast::If) {
        if self.skipped_if.is_some() {
            return;
        }
        // Preserve the original visitor's else-before-then definition precedence.
        // ponytail: O(depth * nodes); use an explicit AST walk if nested files are slow.
        node.else_block().visit(self);
        if let Some(branches) = node.else_if() {
            for branch in branches.iter().rev() {
                branch.block().visit(self);
                branch.condition().visit(self);
            }
        }
        node.block().visit(self);
        node.condition().visit(self);
        self.skipped_if = node.start_position().map(|position| position.bytes());
    }

    fn visit_if_end(&mut self, node: &ast::If) {
        if self.skipped_if == node.start_position().map(|position| position.bytes()) {
            self.skipped_if = None;
        }
    }
}

pub fn parse_component(
    content: &str,
    filename: &str,
    class_name: &str,
    folder_name: &str,
) -> Result<(Vec<String>, Vec<String>)> {
    let mut visitor = Definitions::new(class_name, format!("{folder_name}/{filename}"), true);
    visitor.visit_ast(&parse(content)?);
    Ok((
        visitor
            .fields
            .into_iter()
            .map(|(name, kind)| format!("---@field {name} {kind}"))
            .collect(),
        visitor.functions,
    ))
}

pub fn parse_modutil(content: &str, filename: &str) -> Result<Vec<String>> {
    let mut visitor = Definitions::new("env", filename.into(), false);
    visitor.visit_ast(&parse(content)?);
    Ok(visitor.functions)
}

fn lua_files(directory: &Path, files: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        if entry.file_type()?.is_dir() {
            lua_files(&path, files)?;
        }
        if path.extension().is_some_and(|extension| extension == "lua") {
            files.push(path);
        }
    }
    Ok(())
}

pub fn generate_components(input_dir: impl AsRef<Path>) -> Result<String> {
    let input_dir = input_dir.as_ref();
    ensure!(
        input_dir.is_dir(),
        "not a directory: {}",
        input_dir.display()
    );
    let folder_name = input_dir.file_name().unwrap_or_default().to_string_lossy();
    let mut files = Vec::new();
    lua_files(input_dir, &mut files)?;
    files.sort();
    let mut definitions = Vec::new();
    for path in files {
        let content =
            fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
        let filename = path.file_stem().unwrap_or_default().to_string_lossy();
        let class_name = content
            .lines()
            .rev()
            .find_map(|line| {
                line.trim()
                    .strip_prefix("return ")
                    .and_then(|line| line.split_whitespace().next())
                    .map(|name| name.trim_end_matches(','))
            })
            .unwrap_or(&filename);
        let (fields, functions) = parse_component(&content, &filename, class_name, &folder_name)?;
        let mut lines = vec![format!("---@class {class_name}")];
        lines.extend(fields);
        lines.extend([
            "local _l={}".into(),
            format!("{folder_name}.{filename}=_l"),
            String::new(),
        ]);
        lines.extend(functions);
        lines.push(String::new());
        definitions.push((filename.into_owned(), lines.join("\n")));
    }
    definitions.sort();
    Ok(if definitions.is_empty() {
        String::new()
    } else {
        format!(
            "---@meta\n\n{}",
            definitions
                .into_iter()
                .map(|(_, content)| content)
                .collect::<Vec<_>>()
                .join("\n")
        )
    })
}

pub fn generate_modutil(input_file: impl AsRef<Path>) -> Result<String> {
    let path = input_file.as_ref();
    let content = fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    let definitions = parse_modutil(
        &content,
        &path.file_stem().unwrap_or_default().to_string_lossy(),
    )?;
    Ok(if definitions.is_empty() {
        String::new()
    } else {
        format!("---@meta\n\n{}\n", definitions.join("\n"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_function_returns_do_not_change_outer_annotations() {
        for (declaration, name) in [
            ("function Widget:Outer(value, ...)", "_l:Outer"),
            ("Widget.Outer = function(value, ...)", "_l.Outer"),
            ("env.Outer = function(value, ...)", "Outer"),
        ] {
            for nested in [
                "local helper = function() return true end",
                "local function helper() return true end",
                "function helper() return true end",
                "function Other:Helper() return true end",
            ] {
                for (before, after, annotation) in [
                    ("", "", None),
                    ("if value then return 7 end", "", Some("number")),
                    ("", "return 'outer'", Some("string")),
                ] {
                    let source = format!("{declaration}\n{before}\n{nested}\n{after}\nend");
                    let definitions = if name == "Outer" {
                        parse_modutil(&source, "modutil").unwrap()
                    } else {
                        parse_component(&source, "widget", "Widget", "components")
                            .unwrap()
                            .1
                    };
                    assert_eq!(definitions.len(), 1);
                    let result = &definitions[0];
                    assert!(result.contains("---@param value any\n---@param ... any"));
                    assert!(result.ends_with(&format!("function {name}(value, ...) end")));
                    match annotation {
                        Some(kind) => assert!(result.contains(&format!("---@return {kind}\n"))),
                        None => assert!(!result.contains("---@return")),
                    }
                }
            }
        }
    }

    #[test]
    fn component_fields_methods_and_return_types() {
        let source = "self.any = unknown; self.any = 'first'; self.any = 2\nself.inst = true; self.GetDebugString = 'ignored'\nself.table = {}; self.nilvalue = nil; self.proxy = newproxy(true)\nself.concat = 'a' .. value; self.sum = (1 + 2); self.negative = -1\nself.callback = function() end\nfunction Widget:Method(self, value, ...) self.inside = true; return 1, 'x', false end\nWidget.Ping = function() if yes then return true end return end\nWidget.Ping = function() return 99 end\nfunction Widget:GetDebugString() return 'ignored' end\nfunction Other:Wrong() end\nif yes then self.branch = 1 else self.branch = 'text' end\n";
        let (fields, functions) =
            parse_component(source, "widget", "Widget", "components").unwrap();
        assert_eq!(
            fields,
            [
                "any string",
                "branch string",
                "callback any",
                "concat string",
                "inside boolean",
                "negative any",
                "nilvalue nil",
                "proxy userdata",
                "sum number",
                "table table"
            ]
            .map(|field| format!("---@field {field}"))
        );
        assert_eq!(
            functions,
            [
                "---@source components/widget.lua:6\n---@param value any\n---@param ... any\n---@return number, string, boolean\nfunction _l:Method(self, value, ...) end",
                "---@source components/widget.lua:7\nfunction _l.Ping() end"
            ]
        );
    }

    #[test]
    fn generators_preserve_source_lines_order_and_fail_on_invalid_lua() {
        let directory = tempfile::tempdir().unwrap();
        let components = directory.path().join("components");
        fs::create_dir(&components).unwrap();
        assert_eq!(generate_components(&components).unwrap(), "");
        fs::write(components.join("widget.lua"), "\nlocal Widget = Class(function(self)\n    self.count = 1\nend)\n\nWidget.Ping = function(target)\n    return true\nend\n\nfunction Widget:Pong(target)\n    return true\nend\n\nreturn Widget\n").unwrap();
        let result = generate_components(&components).unwrap();
        assert_eq!(
            result,
            "---@meta\n\n---@class Widget\n---@field count number\nlocal _l={}\ncomponents.widget=_l\n\n---@source components/widget.lua:6\n---@param target any\n---@return boolean\nfunction _l.Ping(target) end\n---@source components/widget.lua:10\n---@param target any\n---@return boolean\nfunction _l:Pong(target) end\n"
        );
        fs::create_dir(components.join("nested")).unwrap();
        fs::write(components.join("nested/a.lua"), "return Alpha\n").unwrap();
        assert!(
            generate_components(&components)
                .unwrap()
                .find("---@class Alpha")
                .unwrap()
                < generate_components(&components)
                    .unwrap()
                    .find("---@class Widget")
                    .unwrap()
        );
        fs::write(components.join("z_invalid.lua"), "local =").unwrap();
        assert!(generate_components(&components).is_err());
        assert!(generate_components(components.join("missing")).is_err());
        let modutil = directory.path().join("modutil.lua");
        fs::write(
            &modutil,
            "\nenv.AddThing = function(name)\n    return true\nend\n",
        )
        .unwrap();
        assert_eq!(
            generate_modutil(&modutil).unwrap(),
            "---@meta\n\n---@source modutil.lua:2\n---@param name any\n---@return boolean\nfunction AddThing(name) end\n"
        );
        fs::write(&modutil, "return {}\n").unwrap();
        assert_eq!(generate_modutil(&modutil).unwrap(), "");
        fs::write(&modutil, "local =").unwrap();
        assert!(generate_modutil(&modutil).is_err());
    }

    #[test]
    fn script_parsing_consumes_all_input_and_preserves_lua_newlines() {
        for source in ["return 1 return 2", "local =", "function Missing("] {
            assert!(parse_modutil(source, "modutil").is_err());
        }
        let source = "env.First = function() return 'a\\\r\nb' end\n\renv.Second = function() return false end";
        let functions = parse_modutil(source, "modutil").unwrap();
        assert_eq!(
            functions[0],
            "---@source modutil.lua:1\n---@return string\nfunction First() end"
        );
        assert_eq!(
            functions[1],
            "---@source modutil.lua:3\n---@return boolean\nfunction Second() end"
        );
        let (fields, functions) = parse_component(
            "(self)[value] = (newproxy)(); Widget.Func = (function() return (7) end)",
            "widget",
            "Widget",
            "components",
        )
        .unwrap();
        assert_eq!(fields, ["---@field value userdata"]);
        assert!(functions[0].contains("---@return number"));
    }
}
