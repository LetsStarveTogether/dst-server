//! Read the static Lua 5.1 data subset used by DST configuration and metadata.

use std::{borrow::Cow, collections::BTreeMap, fmt::Write};

use anyhow::{Context, Result, bail, ensure};
use full_moon::{
    LuaVersion,
    ast::{
        Ast, Call, Expression, Field, FunctionArgs, FunctionCall, LastStmt, Prefix, Stmt, Suffix,
        UnOp,
    },
    tokenizer::{Lexer, LexerResult, StringLiteralQuoteType, Symbol, TokenReference, TokenType},
};
use serde_json::{Map, Number, Value};

pub const MAX_SOURCE_BYTES: usize = 1024 * 1024;
pub const MAX_NESTING: usize = 64;
pub const MAX_TOKENS: usize = 65_536;
pub const MAX_SAFE_INTEGER: i64 = (1_i64 << 53) - 1;

/// Parse one literal; Lua's empty table is represented as a JSON object.
pub fn parse_literal(source: &str) -> Result<Value> {
    ensure!(
        source.len() <= MAX_SOURCE_BYTES - 7,
        "Lua source exceeds the byte limit"
    );
    returned_value(&parse(&format!("return {source}"), false)?)
}

/// Read a returned object, an empty configuration, or KLEI 1 metadata.
/// A single terminal NUL is accepted for native snapshot metadata.
pub fn parse_return_table(source: &str) -> Result<Value> {
    ensure!(
        source.len() <= MAX_SOURCE_BYTES,
        "Lua source exceeds the byte limit"
    );
    let source = source.trim();
    let source = source.strip_suffix('\0').unwrap_or(source).trim_end();
    let source = if let Some(header) = source.strip_prefix("KLEI") {
        ensure!(header.starts_with([' ', '\t']), "invalid KLEI header");
        let header = header.trim_start_matches([' ', '\t']);
        let header = header
            .strip_prefix('1')
            .context("unsupported KLEI version")?;
        ensure!(header.starts_with([' ', '\t']), "invalid KLEI header");
        let payload = header.trim_start_matches([' ', '\t']);
        ensure!(
            payload.starts_with("return"),
            "KLEI metadata requires a return table"
        );
        payload
    } else {
        source
    };
    let ast = parse(source, false)?;
    if ast.nodes().stmts().next().is_none() && ast.nodes().last_stmt().is_none() {
        return Ok(Value::Object(Map::new()));
    }
    let value = returned_value(&ast)?;
    ensure!(
        value.is_object(),
        "Lua return value must be a table with string keys"
    );
    Ok(value)
}

pub(crate) fn normalize_line_endings(source: &str) -> Cow<'_, str> {
    // Lua treats CR, LF, CRLF and LFCR as one newline. Normalize pairs once;
    // full_moon's 5.1 lexer otherwise rejects escaped CRLF/LFCR in short strings.
    if source.contains('\r') {
        let mut normalized = String::with_capacity(source.len());
        let mut characters = source.chars().peekable();
        while let Some(character) = characters.next() {
            if matches!(character, '\r' | '\n') {
                characters.next_if(|next| *next == if character == '\r' { '\n' } else { '\r' });
                normalized.push('\n');
            } else {
                normalized.push(character);
            }
        }
        Cow::Owned(normalized)
    } else {
        Cow::Borrowed(source)
    }
}

/// Literal calls used by native Mod setup files. No Lua is executed.
pub fn literal_calls(source: &str) -> Result<Vec<(String, Vec<Value>)>> {
    let ast = parse(source, true)?;
    let mut calls = Vec::new();
    for statement in ast.nodes().stmts() {
        let Stmt::FunctionCall(call) = statement else {
            bail!("expected literal function calls");
        };
        calls.push(literal_call(call)?);
    }
    if let Some(last) = ast.nodes().last_stmt() {
        let LastStmt::Return(statement) = last else {
            bail!("expected literal function calls");
        };
        for expression in statement.returns() {
            let Expression::FunctionCall(call) = expression else {
                bail!("expected a returned literal function call");
            };
            calls.push(literal_call(call)?);
        }
    }
    Ok(calls)
}

fn literal_call(call: &FunctionCall) -> Result<(String, Vec<Value>)> {
    let Prefix::Name(name) = call.prefix() else {
        bail!("expected a named function");
    };
    let suffixes: Vec<_> = call.suffixes().collect();
    let [Suffix::Call(Call::AnonymousCall(arguments))] = suffixes.as_slice() else {
        bail!("expected one direct function call");
    };
    let arguments = match arguments {
        FunctionArgs::Parentheses { arguments, .. } => arguments
            .iter()
            .map(|argument| value(argument, 0))
            .collect::<Result<Vec<_>>>()?,
        FunctionArgs::String(string_token) => vec![Value::String(string(string_token)?)],
        _ => bail!("unsupported literal function arguments"),
    };
    Ok((name.token().to_string(), arguments))
}

fn parse(source: &str, calls: bool) -> Result<Ast> {
    ensure!(
        source.len() <= MAX_SOURCE_BYTES,
        "Lua source exceeds the byte limit"
    );
    ensure!(
        !source.contains('\0'),
        "Lua source contains an embedded NUL"
    );
    let source = normalize_line_endings(source);
    // Bound nesting before full_moon builds its recursive AST. Rejecting operators
    // also prevents long unary/binary chains from bypassing the nesting limit.
    let mut lexer = Lexer::new(&source, LuaVersion::lua51());
    let mut depth = 0_usize;
    let mut tokens = 0;
    let mut minus = false;
    let mut can_negate = true;
    while let Some(token) = lexer.consume() {
        let LexerResult::Ok(token) = token else {
            bail!("invalid Lua 5.1 syntax");
        };
        tokens += 1;
        ensure!(tokens <= MAX_TOKENS, "Lua source exceeds the token limit");
        let kind = token.token_type();
        ensure!(
            !minus || matches!(kind, TokenType::Number { .. }),
            "minus requires a literal number"
        );
        minus = matches!(
            kind,
            TokenType::Symbol {
                symbol: Symbol::Minus
            }
        );
        ensure!(!minus || can_negate, "only literal Lua data is supported");
        can_negate = matches!(
            kind,
            TokenType::Symbol {
                symbol: Symbol::Return
                    | Symbol::Equal
                    | Symbol::Comma
                    | Symbol::Semicolon
                    | Symbol::LeftBrace
                    | Symbol::LeftBracket
                    | Symbol::LeftParen
            }
        );
        match kind {
            TokenType::Symbol {
                symbol: Symbol::LeftBrace | Symbol::LeftBracket,
            } => {
                depth += 1;
                ensure!(depth <= MAX_NESTING, "Lua nesting exceeds the limit");
            }
            TokenType::Symbol {
                symbol: Symbol::LeftParen,
            } if calls => {
                depth += 1;
                ensure!(depth <= MAX_NESTING, "Lua nesting exceeds the limit");
            }
            TokenType::Symbol {
                symbol: Symbol::RightParen,
            } if calls => {
                depth = depth.checked_sub(1).context("unbalanced Lua call")?;
            }
            TokenType::Symbol {
                symbol: Symbol::RightBrace | Symbol::RightBracket,
            } => {
                depth = depth.checked_sub(1).context("unbalanced Lua table")?;
            }
            TokenType::Symbol {
                symbol:
                    Symbol::Return
                    | Symbol::True
                    | Symbol::False
                    | Symbol::Minus
                    | Symbol::Equal
                    | Symbol::Comma
                    | Symbol::Semicolon,
            }
            | TokenType::Identifier { .. }
            | TokenType::Number { .. }
            | TokenType::Eof => {}
            TokenType::StringLiteral {
                quote_type: StringLiteralQuoteType::Single | StringLiteralQuoteType::Double,
                ..
            } => {}
            _ => bail!("only literal Lua data is supported"),
        }
    }
    let ast = full_moon::parse_fallible(&source, LuaVersion::lua51())
        .into_result()
        .map_err(|_| anyhow::anyhow!("invalid Lua 5.1 syntax"))?;
    ensure!(
        ast.to_string() == source,
        "Lua parser did not consume the complete source"
    );
    Ok(ast)
}

fn returned_value(ast: &Ast) -> Result<Value> {
    ensure!(
        ast.nodes().stmts().next().is_none(),
        "expected one literal Lua return value"
    );
    let Some(LastStmt::Return(statement)) = ast.nodes().last_stmt() else {
        bail!("expected one literal Lua return value");
    };
    ensure!(
        statement.returns().len() == 1,
        "expected one literal Lua return value"
    );
    value(statement.returns().iter().next().unwrap(), 0)
}

fn value(expression: &Expression, depth: usize) -> Result<Value> {
    ensure!(depth <= MAX_NESTING, "Lua nesting exceeds the limit");
    match expression {
        Expression::String(token) => Ok(Value::String(string(token)?)),
        Expression::Number(token) => number(token, false),
        Expression::UnaryOperator {
            unop: UnOp::Minus(_),
            expression,
        } => {
            let Expression::Number(token) = expression.as_ref() else {
                bail!("minus requires a literal number");
            };
            number(token, true)
        }
        Expression::Symbol(token) => match token.token_type() {
            TokenType::Symbol {
                symbol: Symbol::True,
            } => Ok(Value::Bool(true)),
            TokenType::Symbol {
                symbol: Symbol::False,
            } => Ok(Value::Bool(false)),
            _ => bail!("unsupported Lua literal"),
        },
        Expression::TableConstructor(table) => {
            let mut object = Map::new();
            let mut array = BTreeMap::new();
            let mut implicit = 0_u64;
            for field in table.fields() {
                let (key, expression) = match field {
                    Field::NameKey { key, value, .. } => {
                        let TokenType::Identifier { identifier } = key.token_type() else {
                            bail!("expected a literal table key");
                        };
                        (Value::String(identifier.to_string()), value)
                    }
                    Field::ExpressionKey {
                        key,
                        value: expression,
                        ..
                    } => {
                        let key = match key {
                            Expression::String(token) => Value::String(string(token)?),
                            Expression::Number(token) => number(token, false)?,
                            _ => bail!("table keys must be literal strings or positive integers"),
                        };
                        (key, expression)
                    }
                    Field::NoKey(expression) => {
                        implicit += 1;
                        (Value::from(implicit), expression)
                    }
                    _ => bail!("unsupported Lua table field"),
                };
                let item = value(expression, depth + 1)?;
                if let Value::String(key) = key {
                    ensure!(array.is_empty(), "mixed Lua table keys");
                    ensure!(
                        object.insert(key, item).is_none(),
                        "duplicate Lua table key"
                    );
                } else {
                    ensure!(object.is_empty(), "mixed Lua table keys");
                    let index = key
                        .as_u64()
                        .filter(|index| *index > 0)
                        .context("array keys must be positive literal integers")?;
                    ensure!(
                        array.insert(index, item).is_none(),
                        "duplicate Lua table key"
                    );
                }
            }
            if array.is_empty() {
                Ok(Value::Object(object))
            } else {
                ensure!(
                    array.keys().copied().eq(1..=array.len() as u64),
                    "array keys must be consecutive from 1"
                );
                Ok(Value::Array(array.into_values().collect()))
            }
        }
        _ => bail!("only literal Lua values are supported"),
    }
}

fn number(token: &TokenReference, negative: bool) -> Result<Value> {
    let TokenType::Number { text } = token.token_type() else {
        bail!("expected a literal number");
    };
    let text = text.as_str();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        let integer =
            i64::from_str_radix(hex, 16).context("unsupported hexadecimal Lua integer")?;
        ensure!(
            integer <= MAX_SAFE_INTEGER,
            "Lua integer exceeds the safe range"
        );
        return Ok(Value::from(if negative { -integer } else { integer }));
    }
    if !text.contains(['.', 'e', 'E']) {
        let integer: i64 = text.parse().context("unsupported Lua integer")?;
        ensure!(
            integer <= MAX_SAFE_INTEGER,
            "Lua integer exceeds the safe range"
        );
        return Ok(Value::from(if negative { -integer } else { integer }));
    }
    let float: f64 = text.parse().context("unsupported Lua number")?;
    Number::from_f64(if negative { -float } else { float })
        .map(Value::Number)
        .context("Lua number must be finite")
}

pub(crate) fn string(token: &TokenReference) -> Result<String> {
    if let TokenType::StringLiteral {
        literal,
        quote_type: StringLiteralQuoteType::Brackets,
        ..
    } = token.token_type()
    {
        return Ok(literal.strip_prefix('\n').unwrap_or(literal).to_string());
    }
    let TokenType::StringLiteral {
        literal,
        quote_type: StringLiteralQuoteType::Single | StringLiteralQuoteType::Double,
        ..
    } = token.token_type()
    else {
        bail!("expected a quoted Lua string");
    };
    let mut source = literal.as_bytes().iter().copied().peekable();
    let mut decoded = Vec::with_capacity(literal.len());
    while let Some(byte) = source.next() {
        if byte != b'\\' {
            decoded.push(byte);
            continue;
        }
        let escape = source.next().context("incomplete Lua string escape")?;
        match escape {
            b'0'..=b'9' => {
                let mut number = u16::from(escape - b'0');
                for _ in 0..2 {
                    if let Some(digit) = source.next_if(u8::is_ascii_digit) {
                        number = number * 10 + u16::from(digit - b'0');
                    } else {
                        break;
                    }
                }
                decoded.push(u8::try_from(number).context("decimal Lua escape exceeds one byte")?);
            }
            b'\n' | b'\r' => {
                source.next_if(|byte| *byte == if escape == b'\n' { b'\r' } else { b'\n' });
                decoded.push(b'\n');
            }
            b'a' => decoded.push(7),
            b'b' => decoded.push(8),
            b'f' => decoded.push(12),
            b'n' => decoded.push(b'\n'),
            b'r' => decoded.push(b'\r'),
            b't' => decoded.push(b'\t'),
            b'v' => decoded.push(11),
            b'\\' | b'\'' | b'"' => decoded.push(escape),
            _ => bail!("unsupported Lua string escape"),
        }
    }
    String::from_utf8(decoded).context("Lua string must contain valid UTF-8")
}

/// Parse native executable setup code without evaluating it. The lexical bounds
/// also limit recursive expressions before the parser allocates their AST.
pub(crate) fn parse_script(source: &str) -> Result<Ast> {
    ensure!(
        source.len() <= MAX_SOURCE_BYTES,
        "Lua source exceeds the byte limit"
    );
    ensure!(
        !source.contains('\0'),
        "Lua source contains an embedded NUL"
    );
    let source = normalize_line_endings(source);
    let mut lexer = Lexer::new(&source, LuaVersion::lua51());
    let mut depth = 0_usize;
    let mut operators = 0_usize;
    let mut tokens = 0_usize;
    while let Some(token) = lexer.consume() {
        let LexerResult::Ok(token) = token else {
            bail!("invalid Lua 5.1 syntax");
        };
        tokens += 1;
        ensure!(tokens <= MAX_TOKENS, "Lua source exceeds the token limit");
        if let TokenType::Symbol { symbol } = token.token_type() {
            match symbol {
                Symbol::LeftBrace
                | Symbol::LeftBracket
                | Symbol::LeftParen
                | Symbol::Do
                | Symbol::Then
                | Symbol::Function
                | Symbol::Repeat => {
                    depth += 1;
                }
                Symbol::RightBrace
                | Symbol::RightBracket
                | Symbol::RightParen
                | Symbol::End
                | Symbol::ElseIf
                | Symbol::Until => {
                    depth = depth.saturating_sub(1);
                }
                _ => {}
            }
            match symbol {
                Symbol::Plus
                | Symbol::Minus
                | Symbol::Star
                | Symbol::Slash
                | Symbol::Percent
                | Symbol::Caret
                | Symbol::TwoDots
                | Symbol::Hash
                | Symbol::Not
                | Symbol::And
                | Symbol::Or
                | Symbol::TwoEqual
                | Symbol::TildeEqual
                | Symbol::LessThan
                | Symbol::LessThanEqual
                | Symbol::GreaterThan
                | Symbol::GreaterThanEqual => {
                    operators += 1;
                }
                _ => {}
            }
            ensure!(depth <= MAX_NESTING, "Lua nesting exceeds the limit");
            ensure!(
                operators <= MAX_NESTING,
                "Lua setup code exceeds the operator limit"
            );
        }
    }
    let ast = full_moon::parse_fallible(&source, LuaVersion::lua51())
        .into_result()
        .map_err(|_| anyhow::anyhow!("invalid Lua 5.1 syntax"))?;
    ensure!(
        ast.to_string() == source,
        "Lua parser did not consume the complete source"
    );
    Ok(ast)
}

/// Render a finite JSON value as literal Lua data; null has no supported mapping.
pub fn render_literal(value: &Value) -> Result<String> {
    let mut output = String::new();
    render(value, &mut output, 0)?;
    // Keep rendered output within the same grammar and resource limits as input.
    parse_literal(&output)?;
    Ok(output)
}

fn render(value: &Value, output: &mut String, depth: usize) -> Result<()> {
    ensure!(depth <= MAX_NESTING, "Lua nesting exceeds the limit");
    match value {
        Value::Null => bail!("null cannot be represented as literal Lua data"),
        Value::Bool(boolean) => output.push_str(if *boolean { "true" } else { "false" }),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                ensure!(
                    (-MAX_SAFE_INTEGER..=MAX_SAFE_INTEGER).contains(&integer),
                    "Lua integer exceeds the safe range"
                );
            } else if let Some(integer) = number.as_u64() {
                ensure!(
                    integer <= MAX_SAFE_INTEGER as u64,
                    "Lua integer exceeds the safe range"
                );
            }
            write!(output, "{number}")?;
        }
        Value::String(string) => render_string(string, output)?,
        Value::Array(array) => {
            output.push('{');
            for (index, item) in array.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                render(item, output, depth + 1)?;
            }
            output.push('}');
        }
        Value::Object(object) => {
            output.push('{');
            for (index, (key, item)) in object.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push('[');
                render_string(key, output)?;
                output.push_str("]=");
                render(item, output, depth + 1)?;
            }
            output.push('}');
        }
    }
    ensure!(
        output.len() <= MAX_SOURCE_BYTES - 7,
        "Lua output exceeds the byte limit"
    );
    Ok(())
}

fn render_string(string: &str, output: &mut String) -> Result<()> {
    ensure!(
        string.len() <= MAX_SOURCE_BYTES,
        "Lua string exceeds the byte limit"
    );
    output.push('"');
    for character in string.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            character if character.is_control() => {
                let mut buffer = [0; 4];
                for byte in character.encode_utf8(&mut buffer).as_bytes() {
                    write!(output, "\\{byte:03}")?;
                }
            }
            character => output.push(character),
        }
        ensure!(
            output.len() <= MAX_SOURCE_BYTES - 7,
            "Lua output exceeds the byte limit"
        );
    }
    output.push('"');
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn literals_round_trip_with_native_strings_and_number_types() {
        for value in [
            json!(false),
            json!(true),
            json!(0),
            json!(-MAX_SAFE_INTEGER),
            json!(1.25),
            json!(-0.0),
            json!(1.0),
            json!(1e100),
            json!({"values": [1, false, "玩家"]}),
            json!(""),
            json!("quote\"\\"),
            json!("\0\n\r\t"),
            json!("玩家\u{f0001}"),
            json!("a\u{200d}b\u{2028}c"),
        ] {
            let parsed = parse_literal(&render_literal(&value).unwrap()).unwrap();
            assert_eq!(parsed, value);
            if value.is_f64() {
                assert!(parsed.is_f64());
            }
        }
        assert_eq!(parse_literal("{}").unwrap(), json!({}));
        assert_eq!(
            parse_literal("{[2]='two', [1]='one'}").unwrap(),
            json!(["one", "two"])
        );
        assert_eq!(parse_literal("-0XfF").unwrap(), json!(-255));
        assert_eq!(parse_literal(".5").unwrap(), json!(0.5));
    }

    #[test]
    fn escapes_match_lua_51_bytes() {
        assert_eq!(
            parse_literal(r#"'\a\b\f\n\r\t\v\\\'\"'"#).unwrap(),
            json!("\u{7}\u{8}\u{c}\n\r\t\u{b}\\'\"")
        );
        assert_eq!(
            parse_literal(r#"'\231\142\169\229\174\182\000\12\0499'"#).unwrap(),
            json!("玩家\0\u{c}19")
        );
        for newline in ["\n", "\r", "\r\n", "\n\r"] {
            assert_eq!(
                parse_literal(&format!("'left\\{newline}right'")).unwrap(),
                json!("left\nright")
            );
        }
        for code in (0..=31).chain(127..=159) {
            let value = Value::String(char::from_u32(code).unwrap().to_string());
            assert_eq!(
                parse_literal(&render_literal(&value).unwrap()).unwrap(),
                value
            );
        }
    }

    #[test]
    fn executable_ambiguous_or_lossy_data_is_rejected() {
        for source in [
            "require('untrusted')",
            "{value=string.char(10)}",
            "{a=other}",
            "{['same']=1,same=2}",
            r#"{same=1,['\115ame']=2}"#,
            "{[1]='one',[3]='three'}",
            "{[1]='one',name='two'}",
            "{name='two',[1]='one'}",
            "{[1]=1,2}",
            "{[0]=1}",
            "{[-1]=1}",
            "{[1.0]=1}",
            "{[true]=1}",
            "{[name]=1}",
            "nil",
            "{value=nil}",
            "[[long string]]",
            r#"'\256'"#,
            r#"'\255'"#,
            r#"'\128'"#,
            r#"'\x41'"#,
            r#"'\q'"#,
            "'value'; return 'injected'",
            "1,2",
            "9007199254740992",
            "-9007199254740992",
            "0x20000000000000",
            "1e999",
            "-1e999",
            "0x1p2",
            "1+2",
            "- - 1",
            "(1)",
            "'embedded\0nul'",
        ] {
            assert!(parse_literal(source).is_err(), "accepted: {source:?}");
        }
        for value in [
            Value::Null,
            json!({"bad": null}),
            json!(9007199254740992_i64),
            json!(u64::MAX),
        ] {
            assert!(render_literal(&value).is_err());
        }
    }

    #[test]
    fn configuration_and_snapshot_headers_are_strict() {
        assert_eq!(
            parse_return_table(" \nKLEI     1 return {name='房间'}\0\n").unwrap(),
            json!({"name": "房间"})
        );
        assert_eq!(parse_return_table("-- empty").unwrap(), json!({}));
        assert_eq!(
            parse_return_table("--[=[ { return arbitrary text } ]=]\nreturn {clock={cycles=2}}")
                .unwrap(),
            json!({"clock": {"cycles": 2}})
        );
        for source in [
            "KLEI 2 return {}",
            "KLEI1 return {}",
            "KLEI 1return {}",
            "KLEI 1 -- empty",
            "return {}\0\0",
            "return {}\0;return {}",
            "return {1}",
            "value={};return value",
            "return",
        ] {
            assert!(parse_return_table(source).is_err(), "accepted: {source:?}");
        }
    }

    #[test]
    fn bounds_apply_before_recursive_parsing_and_during_rendering() {
        let nested = format!(
            "{}0{}",
            "{".repeat(MAX_NESTING + 1),
            "}".repeat(MAX_NESTING + 1)
        );
        assert!(parse_literal(&nested).is_err());
        assert!(parse_literal(&format!("'{}'", "x".repeat(MAX_SOURCE_BYTES))).is_err());
        assert!(parse_return_table(&" ".repeat(MAX_SOURCE_BYTES + 1)).is_err());
        assert!(parse_literal(&format!("{{{}}}", "1,".repeat(MAX_TOKENS))).is_err());
        assert!(parse_literal(&format!("{}1", "- ".repeat(MAX_TOKENS))).is_err());
        assert!(parse_literal(&format!("1{}", "-1".repeat(MAX_TOKENS))).is_err());
        let mut nested = json!(0);
        for _ in 0..MAX_NESTING + 1 {
            nested = json!([nested]);
        }
        assert!(render_literal(&nested).is_err());
        assert!(render_literal(&json!("\0".repeat(MAX_SOURCE_BYTES / 2))).is_err());
    }
}
