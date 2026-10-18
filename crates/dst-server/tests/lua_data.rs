use dst_server::lua::{MAX_SAFE_INTEGER, parse_literal, render_literal};
use proptest::prelude::*;
use serde_json::Value;

fn literal_value() -> impl Strategy<Value = Value> {
    let scalar = prop_oneof![
        any::<bool>().prop_map(Value::Bool),
        (-MAX_SAFE_INTEGER..=MAX_SAFE_INTEGER).prop_map(Value::from),
        any::<f64>()
            .prop_filter("Lua numbers must be finite", |value| value.is_finite())
            .prop_map(Value::from),
        prop::collection::vec(any::<char>(), 0..48)
            .prop_map(|characters| Value::String(characters.into_iter().collect())),
    ];
    scalar.prop_recursive(4, 32, 4, |inner| {
        prop_oneof![
            // Lua's empty table decodes as an object; arrays need at least one item.
            prop::collection::vec(inner.clone(), 1..5).prop_map(Value::Array),
            prop::collection::btree_map(".{0,24}", inner, 0..5)
                .prop_map(|entries| Value::Object(entries.into_iter().collect())),
        ]
    })
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    #[test]
    fn supported_data_survives_lua_serialization(value in literal_value()) {
        let source = render_literal(&value)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        let decoded = parse_literal(&source)
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        prop_assert_eq!(decoded, value);
    }
}
