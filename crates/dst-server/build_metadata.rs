pub fn resolve(revision: &str, dirty: &str) -> Result<(String, String), &'static str> {
    if !matches!(dirty, "true" | "false" | "unknown") {
        return Err("SOURCE_DIRTY must be true, false or unknown");
    }
    if revision.is_empty() {
        return Ok((String::new(), "unknown".into()));
    }
    if !matches!(revision.len(), 40 | 64) || !revision.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err("SOURCE_REVISION must be a full hexadecimal Git object ID");
    }
    Ok((revision.to_ascii_lowercase(), dirty.into()))
}

#[cfg(test)]
mod tests {
    #[test]
    fn build_identity_requires_explicit_valid_inputs() {
        assert_eq!(
            super::resolve("", "false").unwrap(),
            (String::new(), "unknown".into())
        );
        for dirty in ["true", "false", "unknown"] {
            assert_eq!(
                super::resolve(&"AB".repeat(20), dirty).unwrap(),
                ("ab".repeat(20), dirty.into())
            );
        }
        for revision in ["main", "abcdef", &"x".repeat(40), "bad\nrevision"] {
            assert!(super::resolve(revision, "unknown").is_err());
        }
        assert!(super::resolve(&"a".repeat(40), "clean").is_err());
    }
}
