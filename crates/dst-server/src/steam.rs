//! Typed, anonymous Steam Web API reads, independent of room and Mod policy.
//!
//! Reuse a [`Client`] to share its connection pool. [`Client::published_file_details`]
//! preserves individual Steam failures inside a successful batch.
//!
//! ```no_run
//! use dst_server::steam::{Client, PublishedFileId};
//!
//! # async fn example() -> Result<(), Box<dyn std::error::Error>> {
//! let client = Client::new()?;
//! for file in client.published_file_details(&[PublishedFileId(2189004162)]).await? {
//!     match file.details {
//!         Ok(details) => println!("{}: {} ({})", file.id, details.title, details.updated_at),
//!         Err(code) => eprintln!("{}: {code}", file.id),
//!     }
//! }
//! # Ok(())
//! # }
//! ```

use std::{
    collections::{HashMap, VecDeque},
    fmt,
    str::FromStr,
    time::{Duration, SystemTime},
};

use chrono::{DateTime, Utc};
use reqwest::{StatusCode, Url, header::RETRY_AFTER, redirect::Policy};
use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use tokio::time::Instant;

const PUBLISHED_FILE_DETAILS: &str =
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/";
const OPERATION_TIMEOUT: Duration = Duration::from_secs(30);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;
const MAX_ATTEMPTS: usize = 3;

/// This client's batch bound; larger caller batches must be split explicitly.
pub const MAX_PUBLISHED_FILES: usize = 100;

/// A Workshop item ID. Zero is rejected before sending a request.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PublishedFileId(#[serde(with = "decimal_u64")] pub u64);

impl fmt::Display for PublishedFileId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl FromStr for PublishedFileId {
    type Err = std::num::ParseIntError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        value.parse().map(Self)
    }
}

/// A Steam account identifier, retaining all 64 bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SteamId(#[serde(with = "decimal_u64")] pub u64);

/// A Steam application identifier.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AppId(pub u32);

/// An opaque content handle, not an ordered version number.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct UgcHandle(#[serde(with = "decimal_u64")] pub u64);

/// Steam's EResult value, including codes unknown to this client.
///
/// <https://partner.steamgames.com/doc/api/steam_api#EResult>
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ResultCode(pub i32);

impl ResultCode {
    pub const OK: Self = Self(1);
    pub const FILE_NOT_FOUND: Self = Self(9);
    pub const ACCESS_DENIED: Self = Self(15);
}

impl fmt::Display for ResultCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Steam result {}", self.0)
    }
}

impl std::error::Error for ResultCode {}

/// Workshop visibility, preserving future values without guessing their meaning.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "u32", into = "u32")]
pub enum Visibility {
    Public,
    FriendsOnly,
    Private,
    Unlisted,
    Unknown(u32),
}

impl From<u32> for Visibility {
    fn from(value: u32) -> Self {
        match value {
            0 => Self::Public,
            1 => Self::FriendsOnly,
            2 => Self::Private,
            3 => Self::Unlisted,
            value => Self::Unknown(value),
        }
    }
}

impl From<Visibility> for u32 {
    fn from(value: Visibility) -> Self {
        match value {
            Visibility::Public => 0,
            Visibility::FriendsOnly => 1,
            Visibility::Private => 2,
            Visibility::Unlisted => 3,
            Visibility::Unknown(value) => value,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tag {
    pub tag: String,
}

/// Successful metadata. Missing required fields fail decoding instead of becoming defaults.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PublishedFileDetails {
    pub creator: SteamId,
    pub creator_app_id: AppId,
    pub consumer_app_id: AppId,
    pub filename: String,
    #[serde(with = "decimal_u64")]
    pub file_size: u64,
    /// An empty upstream URL is represented as `None`; URLs are never fetched here.
    #[serde(with = "optional_url")]
    pub file_url: Option<Url>,
    /// Kept verbatim, including native sentinel values; metadata alone does not prove downloadability.
    #[serde(rename = "hcontent_file")]
    pub content_handle: UgcHandle,
    #[serde(with = "optional_url")]
    pub preview_url: Option<Url>,
    #[serde(rename = "hcontent_preview")]
    pub preview_handle: UgcHandle,
    pub title: String,
    pub description: String,
    #[serde(rename = "time_created", with = "chrono::serde::ts_seconds")]
    pub created_at: DateTime<Utc>,
    #[serde(rename = "time_updated", with = "chrono::serde::ts_seconds")]
    pub updated_at: DateTime<Utc>,
    pub visibility: Visibility,
    #[serde(deserialize_with = "deserialize_bool")]
    pub banned: bool,
    pub ban_reason: String,
    pub subscriptions: u64,
    pub favorited: u64,
    pub lifetime_subscriptions: u64,
    pub lifetime_favorited: u64,
    pub views: u64,
    pub tags: Vec<Tag>,
}

/// The ID remains available even when Steam cannot return that item's metadata.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PublishedFile {
    pub id: PublishedFileId,
    pub details: Result<PublishedFileDetails, ResultCode>,
}

/// A transport failure category, without URLs or underlying peer messages.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransportError {
    /// The HTTP client or request could not be constructed.
    Configuration,
    /// A connection could not be established.
    Connection,
    /// The request failed before a response was received.
    Request,
    /// Reading or decompressing the response body failed.
    ResponseBody,
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Configuration => "client or request configuration failed",
            Self::Connection => "connection failed",
            Self::Request => "request failed",
            Self::ResponseBody => "response body read failed",
        })
    }
}

impl std::error::Error for TransportError {}

/// Request failures expose only typed values and fixed messages, never upstream error text.
#[derive(Debug)]
pub enum Error {
    EmptyBatch,
    BatchTooLarge {
        count: usize,
    },
    InvalidPublishedFileId(PublishedFileId),
    Transport(TransportError),
    Http {
        status: StatusCode,
        retry_after: Option<Duration>,
    },
    Timeout,
    ResponseTooLarge,
    Decode {
        line: usize,
        column: usize,
    },
    ItemCountMismatch {
        requested: usize,
        reported: usize,
        received: usize,
    },
    MissingItem(PublishedFileId),
    Api(ResultCode),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyBatch => f.write_str("at least one published file ID is required"),
            Self::BatchTooLarge { count } => {
                write!(
                    f,
                    "requested {count} files; the batch limit is {MAX_PUBLISHED_FILES}"
                )
            }
            Self::InvalidPublishedFileId(id) => write!(f, "invalid published file ID: {id}"),
            Self::Transport(error) => write!(f, "Steam HTTP {error}"),
            Self::Http { status, .. } => write!(f, "Steam returned HTTP {status}"),
            Self::Timeout => f.write_str("Steam request timed out"),
            Self::ResponseTooLarge => {
                write!(f, "Steam response exceeds {MAX_RESPONSE_BYTES} bytes")
            }
            Self::Decode { line, column } => {
                write!(f, "invalid Steam response at line {line}, column {column}")
            }
            Self::ItemCountMismatch {
                requested,
                reported,
                received,
            } => {
                write!(
                    f,
                    "Steam item count mismatch: requested {requested}, reported {reported}, received {received}"
                )
            }
            Self::MissingItem(id) => write!(f, "Steam response is missing requested item {id}"),
            Self::Api(code) => code.fmt(f),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Transport(error) => Some(error),
            Self::Api(code) => Some(code),
            _ => None,
        }
    }
}

impl Error {
    fn transport(error: reqwest::Error) -> Self {
        if error.is_timeout() {
            return Self::Timeout;
        }
        Self::Transport(if error.is_builder() {
            TransportError::Configuration
        } else if error.is_connect() {
            TransportError::Connection
        } else if error.is_body() || error.is_decode() {
            // reqwest also classifies truncated response streams as decode errors.
            TransportError::ResponseBody
        } else {
            TransportError::Request
        })
    }

    fn retryable(&self) -> bool {
        match self {
            Self::Timeout => true,
            Self::Transport(error) => *error != TransportError::Configuration,
            Self::Http { status, .. } => {
                matches!(status.as_u16(), 408 | 429 | 500 | 502 | 503 | 504)
            }
            _ => false,
        }
    }
}

/// An anonymous client with a shared connection pool and bounded requests.
#[derive(Clone, Debug)]
pub struct Client {
    http: reqwest::Client,
    endpoint: String,
}

impl Client {
    pub fn new() -> Result<Self, Error> {
        Self::with_proxy(None)
    }

    /// Use an explicit proxy when supplied; otherwise retain the HTTP client's defaults.
    pub fn with_proxy(proxy: Option<&str>) -> Result<Self, Error> {
        let mut http = reqwest::Client::builder()
            .redirect(Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(Duration::from_secs(3))
            .timeout(REQUEST_TIMEOUT)
            .user_agent(concat!("dst-server/", env!("CARGO_PKG_VERSION")));
        if let Some(proxy) = proxy {
            http = http.proxy(reqwest::Proxy::all(proxy).map_err(Error::transport)?);
        }
        Ok(Self {
            http: http.build().map_err(Error::transport)?,
            endpoint: PUBLISHED_FILE_DETAILS.into(),
        })
    }

    /// Query up to [`MAX_PUBLISHED_FILES`] IDs without a Steam token.
    ///
    /// Results follow request order, including explicitly repeated IDs.
    /// Unknown fields are ignored; missing, extra or mismatched items fail the batch.
    /// Item failures remain in [`PublishedFile::details`].
    ///
    /// This read-only POST retries temporary transport and HTTP failures at most twice,
    /// honors `Retry-After` within the call, and has a 30 second budget including body reads and decoding.
    /// Synchronous decoding runs to completion; a late result is returned as [`Error::Timeout`].
    ///
    /// <https://partner.steamgames.com/doc/webapi/ISteamRemoteStorage#GetPublishedFileDetails>
    pub async fn published_file_details(
        &self,
        ids: &[PublishedFileId],
    ) -> Result<Vec<PublishedFile>, Error> {
        if ids.is_empty() {
            return Err(Error::EmptyBatch);
        }
        if ids.len() > MAX_PUBLISHED_FILES {
            return Err(Error::BatchTooLarge { count: ids.len() });
        }
        if let Some(id) = ids.iter().find(|id| id.0 == 0) {
            return Err(Error::InvalidPublishedFileId(*id));
        }
        let deadline = Instant::now() + OPERATION_TIMEOUT;
        let mut params = vec![("itemcount".to_owned(), ids.len().to_string())];
        params.extend(
            ids.iter()
                .enumerate()
                .map(|(index, id)| (format!("publishedfileids[{index}]"), id.to_string())),
        );
        let result = tokio::time::timeout_at(deadline, async {
            for attempt in 0..MAX_ATTEMPTS {
                let error = match self.request(&params).await {
                    Ok(bytes) => return decode(&bytes, ids),
                    Err(error) => error,
                };
                if !error.retryable() || attempt + 1 == MAX_ATTEMPTS {
                    return Err(error);
                }
                let retry_after = match error {
                    Error::Http { retry_after, .. } => retry_after,
                    _ => None,
                };
                let delay = retry_after.unwrap_or(Duration::from_millis(250 << attempt));
                if delay >= deadline.saturating_duration_since(Instant::now()) {
                    return Err(error);
                }
                tokio::time::sleep(delay).await;
            }
            unreachable!("the final attempt always returns")
        })
        .await;
        match result {
            Ok(result) if Instant::now() < deadline => result,
            _ => Err(Error::Timeout),
        }
    }

    async fn request(&self, params: &[(String, String)]) -> Result<Vec<u8>, Error> {
        let mut response = self
            .http
            .post(&self.endpoint)
            .form(params)
            .send()
            .await
            .map_err(Error::transport)?;
        let status = response.status();
        if !status.is_success() {
            let retry_after = response.headers().get(RETRY_AFTER).and_then(|value| {
                let value = value.to_str().ok()?.trim();
                if let Ok(seconds) = value.parse::<u64>() {
                    return Some(Duration::from_secs(seconds));
                }
                httpdate::parse_http_date(value)
                    .ok()
                    .map(|date| date.duration_since(SystemTime::now()).unwrap_or_default())
            });
            return Err(Error::Http {
                status,
                retry_after,
            });
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            return Err(Error::ResponseTooLarge);
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(Error::transport)? {
            if chunk.len() > MAX_RESPONSE_BYTES.saturating_sub(body.len()) {
                return Err(Error::ResponseTooLarge);
            }
            body.extend_from_slice(&chunk);
        }
        Ok(body)
    }
}

#[derive(Deserialize)]
struct Envelope {
    response: ApiResult<Batch>,
}

#[derive(Deserialize)]
struct Batch {
    resultcount: usize,
    publishedfiledetails: Vec<FileResponse>,
}

#[derive(Deserialize)]
struct FileResponse {
    #[serde(rename = "publishedfileid")]
    id: PublishedFileId,
    #[serde(flatten)]
    result: ApiResult<PublishedFileDetails>,
}

// Both the batch and each item carry their own numeric success discriminator.
#[derive(Deserialize)]
#[serde(untagged)]
enum ApiResult<T> {
    Success {
        #[serde(rename = "result")]
        _success: Success,
        #[serde(flatten)]
        data: T,
    },
    Failure {
        result: Failure,
    },
}

impl<T> ApiResult<T> {
    fn into_result(self) -> Result<T, ResultCode> {
        match self {
            Self::Success { data, .. } => Ok(data),
            Self::Failure { result } => Err(result.0),
        }
    }
}

struct Success;
struct Failure(ResultCode);

impl<'de> Deserialize<'de> for Success {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        if ResultCode::deserialize(deserializer)? == ResultCode::OK {
            Ok(Self)
        } else {
            Err(de::Error::custom("expected a successful Steam result"))
        }
    }
}

impl<'de> Deserialize<'de> for Failure {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let code = ResultCode::deserialize(deserializer)?;
        if code == ResultCode::OK {
            Err(de::Error::custom("expected a failed Steam result"))
        } else {
            Ok(Self(code))
        }
    }
}

fn decode(bytes: &[u8], ids: &[PublishedFileId]) -> Result<Vec<PublishedFile>, Error> {
    let envelope: Envelope = serde_json::from_slice(bytes).map_err(|error| Error::Decode {
        line: error.line(),
        column: error.column(),
    })?;
    let batch = envelope.response.into_result().map_err(Error::Api)?;
    if batch.resultcount != ids.len() || batch.publishedfiledetails.len() != ids.len() {
        return Err(Error::ItemCountMismatch {
            requested: ids.len(),
            reported: batch.resultcount,
            received: batch.publishedfiledetails.len(),
        });
    }
    let mut by_id: HashMap<PublishedFileId, VecDeque<PublishedFile>> = HashMap::new();
    for file in batch.publishedfiledetails {
        by_id.entry(file.id).or_default().push_back(PublishedFile {
            id: file.id,
            details: file.result.into_result(),
        });
    }
    ids.iter()
        .map(|id| {
            by_id
                .get_mut(id)
                .and_then(VecDeque::pop_front)
                .ok_or(Error::MissingItem(*id))
        })
        .collect()
}

mod decimal_u64 {
    use super::*;

    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(value)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Integer {
            Number(u64),
            Decimal(String),
        }
        match Integer::deserialize(deserializer)? {
            Integer::Number(value) => Ok(value),
            Integer::Decimal(value)
                if !value.is_empty() && value.bytes().all(|c| c.is_ascii_digit()) =>
            {
                value
                    .parse()
                    .map_err(|_| de::Error::custom("integer exceeds uint64"))
            }
            _ => Err(de::Error::custom("expected an unsigned decimal integer")),
        }
    }
}

mod optional_url {
    use super::*;

    pub fn serialize<S: Serializer>(value: &Option<Url>, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(value.as_ref().map_or("", Url::as_str))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Url>, D::Error> {
        let value = String::deserialize(deserializer)?;
        if value.is_empty() {
            Ok(None)
        } else {
            Url::parse(&value)
                .map(Some)
                .map_err(|_| de::Error::custom("invalid URL"))
        }
    }
}

fn deserialize_bool<'de, D: Deserializer<'de>>(deserializer: D) -> Result<bool, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Boolean {
        Bool(bool),
        Integer(u8),
    }
    match Boolean::deserialize(deserializer)? {
        Boolean::Bool(value) => Ok(value),
        Boolean::Integer(0) => Ok(false),
        Boolean::Integer(1) => Ok(true),
        _ => Err(de::Error::custom("expected a boolean or 0/1")),
    }
}

#[cfg(test)]
pub(crate) mod tests;
