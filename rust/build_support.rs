//! Strict structural parsing shared by the migration build script and tests.

use std::collections::BTreeMap;

use serde::de::{self, Deserialize, Deserializer, MapAccess, Visitor};

#[derive(Debug)]
struct ChecksumManifest(BTreeMap<String, String>);

impl<'de> Deserialize<'de> for ChecksumManifest {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct ManifestVisitor;

        impl<'de> Visitor<'de> for ManifestVisitor {
            type Value = ChecksumManifest;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("an object containing one files object")
            }

            fn visit_map<M>(self, mut map: M) -> Result<Self::Value, M::Error>
            where
                M: MapAccess<'de>,
            {
                let mut files = None;
                while let Some(key) = map.next_key::<String>()? {
                    if key != "files" {
                        return Err(de::Error::custom(format!(
                            "unexpected checksum manifest field: {key}"
                        )));
                    }
                    if files.is_some() {
                        return Err(de::Error::custom("duplicate checksum manifest files field"));
                    }
                    files = Some(map.next_value::<ChecksumFiles>()?);
                }
                files
                    .map(|files| ChecksumManifest(files.0))
                    .ok_or_else(|| de::Error::custom("checksum manifest is missing files"))
            }
        }

        deserializer.deserialize_map(ManifestVisitor)
    }
}

#[derive(Debug)]
struct ChecksumFiles(BTreeMap<String, String>);

impl<'de> Deserialize<'de> for ChecksumFiles {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct FilesVisitor;

        impl<'de> Visitor<'de> for FilesVisitor {
            type Value = ChecksumFiles;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("an object mapping SQL migration names to SHA-256 strings")
            }

            fn visit_map<M>(self, mut map: M) -> Result<Self::Value, M::Error>
            where
                M: MapAccess<'de>,
            {
                let mut files = BTreeMap::new();
                while let Some(name) = map.next_key::<String>()? {
                    if !name.ends_with(".sql") {
                        return Err(de::Error::custom(format!(
                            "checksum manifest contains non-SQL entry: {name}"
                        )));
                    }
                    if files.contains_key(&name) {
                        return Err(de::Error::custom(format!(
                            "duplicate canonical migration checksum: {name}"
                        )));
                    }
                    let checksum = map.next_value::<String>()?;
                    validate_checksum(&name, &checksum).map_err(de::Error::custom)?;
                    files.insert(name, checksum);
                }
                if files.is_empty() {
                    return Err(de::Error::custom(
                        "canonical migration checksum manifest is empty",
                    ));
                }
                Ok(ChecksumFiles(files))
            }
        }

        deserializer.deserialize_map(FilesVisitor)
    }
}

fn validate_checksum(name: &str, checksum: &str) -> Result<(), String> {
    if checksum.len() != 64 || !checksum.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!(
            "invalid SHA-256 checksum for canonical migration: {name}"
        ));
    }
    Ok(())
}

pub fn parse_checksums(manifest: &str) -> BTreeMap<String, String> {
    serde_json::from_str::<ChecksumManifest>(manifest)
        .unwrap_or_else(|error| panic!("invalid canonical migration checksum manifest: {error}"))
        .0
}
