//! Bounded reads from Klei's build, release, lobby and room services.

use std::{collections::BTreeMap, net::Ipv4Addr, str::FromStr, sync::LazyLock, time::Duration};

use chrono::NaiveDate;
use futures::{StreamExt, stream};
use regex::Regex;
use reqwest::{Client, RequestBuilder, Url, redirect::Policy};
use scraper::{Html, Selector};
use serde::{Deserialize, Deserializer, Serialize, de::DeserializeOwned};
use serde_json::{Map, Value, json};

use crate::{
    lua,
    model::{Error, ErrorCode, Outcome, Result},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Region {
    #[serde(rename = "us-east-1")]
    UsEast,
    #[serde(rename = "eu-central-1")]
    EuCentral,
    #[serde(rename = "ap-southeast-1")]
    ApSoutheast,
    #[serde(rename = "ap-east-1")]
    ApEast,
}

impl Region {
    pub const ALL: [Self; 4] = [
        Self::UsEast,
        Self::EuCentral,
        Self::ApSoutheast,
        Self::ApEast,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::UsEast => "us-east-1",
            Self::EuCentral => "eu-central-1",
            Self::ApSoutheast => "ap-southeast-1",
            Self::ApEast => "ap-east-1",
        }
    }
}

impl FromStr for Region {
    type Err = Error;

    fn from_str(value: &str) -> Result<Self> {
        Self::ALL
            .into_iter()
            .find(|region| region.as_str() == value)
            .ok_or_else(|| Error::invalid("region", "unsupported Klei region"))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Platform {
    Steam,
    #[serde(rename = "PSN")]
    Psn,
    Rail,
    #[serde(rename = "XBone")]
    Xbone,
    Switch,
}

impl Platform {
    pub const ALL: [Self; 5] = [
        Self::Steam,
        Self::Psn,
        Self::Rail,
        Self::Xbone,
        Self::Switch,
    ];

    pub fn lobby_name(self) -> &'static str {
        match self {
            Self::Steam => "Steam",
            Self::Psn => "PSN",
            Self::Rail => "Rail",
            Self::Xbone => "XBone",
            Self::Switch => "Switch",
        }
    }
}

impl FromStr for Platform {
    type Err = Error;

    fn from_str(value: &str) -> Result<Self> {
        Self::ALL
            .into_iter()
            .find(|platform| platform.lobby_name().eq_ignore_ascii_case(value))
            .ok_or_else(|| Error::invalid("platform", "unsupported Klei platform"))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Player {
    pub name: String,
    pub netid: String,
    /// Custom Mod characters are retained without a closed character enum.
    pub prefab: String,
    pub colour: String,
    pub eventlevel: i64,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Secondary {
    pub id: String,
    pub port: Option<u16>,
    #[serde(rename = "__addr", alias = "addr")]
    pub addr: Option<Ipv4Addr>,
    pub steamid: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Lobby {
    #[serde(rename = "__rowId", alias = "row_id")]
    pub row_id: String,
    pub name: String,
    #[serde(rename = "__addr", alias = "addr")]
    pub addr: Ipv4Addr,
    pub port: u16,
    pub host: String,
    pub connected: u32,
    pub maxconnections: u32,
    pub v: u64,
    pub allownewplayers: bool,
    pub clanonly: bool,
    pub clienthosted: bool,
    pub dedicated: bool,
    pub fo: bool,
    pub lanonly: bool,
    pub mods: bool,
    pub password: bool,
    pub pvp: bool,
    pub serverpaused: bool,
    /// Klei's platform bit mask, including any combinations reported upstream.
    pub platform: u32,
    pub session: String,
    pub guid: String,
    pub intent: String,
    pub steamroom: String,
    pub region: Region,
    pub tags: Option<String>,
    pub mode: Option<String>,
    pub season: Option<String>,
    pub steamclanid: Option<String>,
    pub ownernetid: Option<String>,
    pub steamid: Option<String>,
    pub secondaries: Option<BTreeMap<String, Secondary>>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Lobby {
    pub fn connect_code(&self) -> String {
        format!("c_connect('{}', {})", self.addr, self.port)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Room {
    #[serde(flatten)]
    pub lobby: Lobby,
    pub tick: u64,
    pub clientmodsoff: bool,
    pub nat: i64,
    pub data: Option<String>,
    pub worldgen: Option<String>,
    pub mods_info: Option<Vec<Value>>,
    #[serde(default, deserialize_with = "deserialize_players")]
    pub players: Vec<Player>,
    pub desc: Option<String>,
}

/// Accept Klei's static Lua player arrays or their already decoded JSON form.
pub fn parse_players(value: Value) -> Result<Vec<Player>> {
    let value = match value {
        Value::Null => return Ok(Vec::new()),
        Value::String(source) => {
            let source = source.trim();
            if source.is_empty() {
                return Ok(Vec::new());
            }
            let source = source
                .strip_prefix("return")
                .filter(|rest| {
                    rest.chars()
                        .next()
                        .is_none_or(|character| !character.is_alphanumeric() && character != '_')
                })
                .unwrap_or(source);
            lua::parse_literal(source).map_err(|_| {
                Error::new(
                    ErrorCode::Protocol,
                    "Klei players must contain static Lua data",
                )
            })?
        }
        value => value,
    };
    if value.as_object().is_some_and(Map::is_empty) {
        return Ok(Vec::new());
    }
    if !value.is_array() {
        return Err(Error::new(
            ErrorCode::Protocol,
            "Klei players must be a literal array",
        ));
    }
    serde_json::from_value(value).map_err(|_| {
        Error::new(
            ErrorCode::Protocol,
            "Klei player does not match the player schema",
        )
    })
}

fn deserialize_players<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Vec<Player>, D::Error> {
    parse_players(Value::deserialize(deserializer)?).map_err(serde::de::Error::custom)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum VersionType {
    Release,
    Test,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Version {
    pub number: u64,
    #[serde(rename = "type")]
    pub kind: VersionType,
    pub date: NaiveDate,
    pub url: String,
    pub row_id: Option<u64>,
    pub release_id: Option<u64>,
    pub is_current_release: bool,
    pub is_hotfix: bool,
}

pub fn parse_versions(html: &str) -> Result<Vec<Version>> {
    static ROW: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse("li.cCmsRecord_row").unwrap());
    static LINK: LazyLock<Selector> = LazyLock::new(|| Selector::parse("a.cRelease").unwrap());
    static HEADING: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse("h3.ipsType_sectionHead").unwrap());
    static BADGE: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse("h3.ipsType_sectionHead span.ipsBadge").unwrap());
    static META: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse(".ipsDataItem_meta").unwrap());
    static HOTFIX: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse(".cUpdate_hotfix").unwrap());
    static NUMBER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b[0-9]+\b").unwrap());
    static DATE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{2})").unwrap());
    let malformed = || {
        Error::new(
            ErrorCode::Protocol,
            "Klei version row is missing or has invalid required fields",
        )
    };
    let document = Html::parse_document(html);
    let mut versions = Vec::new();
    for row in document.select(&ROW) {
        let link = row.select(&LINK).next().ok_or_else(malformed)?;
        let heading = row
            .select(&HEADING)
            .next()
            .ok_or_else(malformed)?
            .text()
            .collect::<Vec<_>>()
            .join(" ");
        let meta = row
            .select(&META)
            .next()
            .ok_or_else(malformed)?
            .text()
            .collect::<Vec<_>>()
            .join(" ");
        let badge = row
            .select(&BADGE)
            .next()
            .ok_or_else(malformed)?
            .text()
            .collect::<String>();
        let date = DATE.captures(&meta).ok_or_else(malformed)?;
        let date = NaiveDate::from_ymd_opt(
            2000 + date[3].parse::<i32>().map_err(|_| malformed())?,
            date[1].parse().map_err(|_| malformed())?,
            date[2].parse().map_err(|_| malformed())?,
        )
        .ok_or_else(malformed)?;
        let optional_int = |value: Option<&str>| {
            value.and_then(|value| value.replace(',', "").trim().parse().ok())
        };
        versions.push(Version {
            number: NUMBER
                .find(&heading)
                .ok_or_else(malformed)?
                .as_str()
                .parse()
                .map_err(|_| malformed())?,
            kind: match badge.trim() {
                "Release" => VersionType::Release,
                "Test" => VersionType::Test,
                _ => return Err(malformed()),
            },
            date,
            url: link.value().attr("href").ok_or_else(malformed)?.into(),
            row_id: optional_int(row.value().attr("data-rowid")),
            release_id: optional_int(link.value().attr("data-releaseid")),
            is_current_release: link.value().attr("data-currentrelease").is_some(),
            is_hotfix: row.select(&HOTFIX).next().is_some(),
        });
    }
    versions.sort_by_key(|version| std::cmp::Reverse((version.date, version.number)));
    Ok(versions)
}

#[derive(Debug, Clone)]
pub struct KleiEndpoints {
    pub builds: String,
    pub versions: String,
    pub regions: String,
    pub lobby: String,
    pub room: String,
}

impl Default for KleiEndpoints {
    fn default() -> Self {
        Self {
            builds: "https://s3.amazonaws.com/dstbuilds/builds.json".into(),
            versions: "https://kleiforums.com/game-updates/dst/".into(),
            regions: "https://lobby-v2-cdn.klei.com/regioncapabilities-v2.json".into(),
            lobby: "https://lobby-v2-cdn.klei.com/{region}-{platform}.json.gz".into(),
            room: "https://lobby-v2-{region}.klei.com/lobby/read".into(),
        }
    }
}

/// Endpoint overrides are explicit and trusted, including their access to the
/// room token. Redirects and ambient proxy configuration are always disabled.
#[derive(Clone)]
pub struct KleiConfig {
    pub access_token: Option<String>,
    pub endpoints: KleiEndpoints,
    pub request_timeout: Duration,
    pub connect_timeout: Duration,
    pub max_response_bytes: usize,
    pub lobby_concurrency: usize,
    pub room_concurrency: usize,
}

impl Default for KleiConfig {
    fn default() -> Self {
        Self {
            access_token: None,
            endpoints: KleiEndpoints::default(),
            request_timeout: Duration::from_secs(30),
            connect_timeout: Duration::from_secs(10),
            max_response_bytes: 32 * 1024 * 1024,
            lobby_concurrency: 8,
            room_concurrency: 24,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct LobbyQuery {
    pub region: Region,
    pub platform: Platform,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RoomQuery {
    pub row_id: String,
    pub region: Region,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueryResult<Q, T> {
    pub query: Q,
    pub result: Outcome<T>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RoomDiscovery {
    /// Retained so an unavailable lobby cannot silently hide undiscovered rooms.
    pub lobbies: Vec<QueryResult<LobbyQuery, Vec<Lobby>>>,
    pub rooms: Vec<QueryResult<RoomQuery, Option<Room>>>,
}

pub struct KleiClient {
    client: Client,
    config: KleiConfig,
}

impl KleiClient {
    pub fn new(config: KleiConfig) -> Result<Self> {
        if config.lobby_concurrency == 0
            || config.room_concurrency == 0
            || config.max_response_bytes == 0
        {
            return Err(Error::invalid(
                "klei",
                "concurrency and body limits must be positive",
            ));
        }
        if config.request_timeout.is_zero()
            || config.connect_timeout.is_zero()
            || std::time::Instant::now()
                .checked_add(config.request_timeout)
                .is_none()
            || std::time::Instant::now()
                .checked_add(config.connect_timeout)
                .is_none()
        {
            return Err(Error::invalid(
                "klei",
                "HTTP timeouts are outside the supported range",
            ));
        }
        if config
            .access_token
            .as_ref()
            .is_some_and(|token| token.trim().is_empty() || token.len() > 64 * 1024)
        {
            return Err(Error::invalid(
                "access_token",
                "Klei access token is empty or exceeds the size limit",
            ));
        }
        for endpoint in [
            &config.endpoints.builds,
            &config.endpoints.versions,
            &config.endpoints.regions,
            &config.endpoints.lobby,
            &config.endpoints.room,
        ] {
            endpoint_url(
                &endpoint
                    .replace("{region}", Region::UsEast.as_str())
                    .replace("{platform}", Platform::Steam.lobby_name()),
            )?;
        }
        let client = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .retry(reqwest::retry::never())
            .timeout(config.request_timeout)
            .connect_timeout(config.connect_timeout)
            .user_agent(concat!("dst-server/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(http_error)?;
        Ok(Self { client, config })
    }

    pub async fn get_latest_build(&self, version_type: &str) -> Result<u64> {
        let bytes = self
            .read(
                self.client
                    .get(endpoint_url(&self.config.endpoints.builds)?),
            )
            .await?;
        let builds: BTreeMap<String, Vec<Value>> = decode(&bytes, "builds")?;
        let mut latest = None;
        for (kind, versions) in builds {
            for version in versions {
                let version = version
                    .as_u64()
                    .or_else(|| version.as_str().and_then(|value| value.trim().parse().ok()))
                    .ok_or_else(|| {
                        Error::new(
                            ErrorCode::Protocol,
                            "Klei build numbers must be nonnegative integers",
                        )
                    })?;
                if kind == version_type {
                    latest = Some(latest.unwrap_or(0).max(version));
                }
            }
        }
        latest.ok_or_else(|| {
            Error::new(
                ErrorCode::Protocol,
                "Klei build response has no versions for the requested branch",
            )
        })
    }

    pub async fn get_versions(&self) -> Result<Vec<Version>> {
        let bytes = self
            .read(
                self.client
                    .get(endpoint_url(&self.config.endpoints.versions)?),
            )
            .await?;
        let html = std::str::from_utf8(&bytes)
            .map_err(|_| Error::new(ErrorCode::Protocol, "Klei version page is not UTF-8"))?;
        parse_versions(html)
    }

    pub async fn get_regions(&self) -> Result<Vec<String>> {
        #[derive(Deserialize)]
        struct Capabilities {
            #[serde(rename = "LobbyRegions", default)]
            regions: Vec<LobbyRegion>,
        }
        #[derive(Deserialize)]
        struct LobbyRegion {
            #[serde(rename = "Region")]
            region: String,
        }
        let bytes = self
            .read(
                self.client
                    .get(endpoint_url(&self.config.endpoints.regions)?),
            )
            .await?;
        Ok(decode::<Capabilities>(&bytes, "regions")?
            .regions
            .into_iter()
            .map(|region| region.region)
            .collect())
    }

    pub async fn lobby(&self, region: Region, platform: Platform) -> Result<Vec<Lobby>> {
        let endpoint = self
            .config
            .endpoints
            .lobby
            .replace("{region}", region.as_str())
            .replace("{platform}", platform.lobby_name());
        let bytes = self.read(self.client.get(endpoint_url(&endpoint)?)).await?;
        decode_rows(&bytes, region)
    }

    pub async fn room(&self, row_id: &str, region: Region) -> Result<Option<Room>> {
        let token = self.require_token()?;
        if row_id.is_empty() || row_id.len() > 4096 || row_id.contains('\0') {
            return Err(Error::invalid(
                "row_id",
                "Klei row identifier is empty or exceeds the permitted bounds",
            ));
        }
        let endpoint = self
            .config
            .endpoints
            .room
            .replace("{region}", region.as_str());
        let payload = json!({"__gameId": "DontStarveTogether", "__token": token, "query": {"__rowId": row_id}});
        let bytes = self
            .read(self.client.post(endpoint_url(&endpoint)?).json(&payload))
            .await?;
        Ok(decode_rows::<Room>(&bytes, region)?.into_iter().next())
    }

    /// Each region/platform result remains visible, including a failed request.
    pub fn get_lobbies(
        &self,
        regions: &[Region],
        platforms: &[Platform],
    ) -> impl Future<Output = Vec<QueryResult<LobbyQuery, Vec<Lobby>>>> + Send + '_ {
        let regions = regions.to_vec();
        let platforms = platforms.to_vec();
        async move {
            let queries = regions.into_iter().flat_map(move |region| {
                platforms
                    .clone()
                    .into_iter()
                    .map(move |platform| LobbyQuery { region, platform })
            });
            let mut results: Vec<_> = stream::iter(queries.enumerate())
                .map(|(index, query)| async move {
                    let result = self.lobby(query.region, query.platform).await.into();
                    (index, QueryResult { query, result })
                })
                .buffer_unordered(self.config.lobby_concurrency)
                .collect()
                .await;
            results.sort_by_key(|(index, _)| *index);
            results.into_iter().map(|(_, result)| result).collect()
        }
    }

    /// Iterators are consumed only as request slots become available. A slow
    /// first room does not prevent later requests from finishing.
    pub async fn get_rooms<I: IntoIterator<Item = RoomQuery>>(
        &self,
        rooms: I,
    ) -> Result<Vec<QueryResult<RoomQuery, Option<Room>>>> {
        self.require_token()?;
        let mut results: Vec<_> = stream::iter(rooms.into_iter().enumerate())
            .map(|(index, query)| async move {
                let result = self.room(&query.row_id, query.region).await.into();
                (index, QueryResult { query, result })
            })
            .buffer_unordered(self.config.room_concurrency)
            .collect()
            .await;
        results.sort_by_key(|(index, _)| *index);
        Ok(results.into_iter().map(|(_, result)| result).collect())
    }

    pub async fn discover_rooms(&self) -> Result<RoomDiscovery> {
        self.require_token()?;
        let lobbies = self.get_lobbies(&Region::ALL, &Platform::ALL).await;
        let queries: Vec<_> = lobbies
            .iter()
            .flat_map(|result| match &result.result {
                Outcome::Success { value } => value.as_slice(),
                Outcome::Failure { .. } => &[],
            })
            .map(|lobby| RoomQuery {
                row_id: lobby.row_id.clone(),
                region: lobby.region,
            })
            .collect();
        let rooms = self.get_rooms(queries).await?;
        Ok(RoomDiscovery { lobbies, rooms })
    }

    fn require_token(&self) -> Result<&str> {
        self.config.access_token.as_deref().ok_or_else(|| {
            Error::invalid(
                "access_token",
                "a Klei access token is required to query room details",
            )
        })
    }

    async fn read(&self, request: RequestBuilder) -> Result<Vec<u8>> {
        let mut response = request.send().await.map_err(http_error)?;
        if !response.status().is_success() {
            return Err(Error::new(
                ErrorCode::Transport,
                "Klei request returned an unsuccessful HTTP status",
            )
            .with_details(json!({"status": response.status().as_u16()})));
        }
        let limit = self.config.max_response_bytes;
        if response
            .content_length()
            .is_some_and(|length| length > limit as u64)
        {
            return Err(Error::new(
                ErrorCode::Overflow,
                "Klei response exceeds the byte limit",
            ));
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(http_error)? {
            if chunk.len() > limit.saturating_sub(body.len()) {
                return Err(Error::new(
                    ErrorCode::Overflow,
                    "Klei response exceeds the byte limit",
                ));
            }
            body.extend_from_slice(&chunk);
        }
        Ok(body)
    }
}

fn endpoint_url(endpoint: &str) -> Result<Url> {
    let url = Url::parse(endpoint)
        .map_err(|_| Error::invalid("endpoint", "Klei endpoint must be an absolute HTTP URL"))?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(Error::invalid(
            "endpoint",
            "Klei endpoint must be an HTTP URL without embedded credentials or fragments",
        ));
    }
    Ok(url)
}

fn http_error(error: reqwest::Error) -> Error {
    Error::new(
        if error.is_timeout() {
            ErrorCode::Timeout
        } else {
            ErrorCode::Transport
        },
        format!("Klei HTTP request failed: {}", error.without_url()),
    )
}

fn decode<T: DeserializeOwned>(bytes: &[u8], kind: &str) -> Result<T> {
    serde_json::from_slice(bytes).map_err(|error| {
        Error::new(
            ErrorCode::Protocol,
            format!("Klei {kind} response does not match its schema"),
        )
        .with_details(json!({"line": error.line(), "column": error.column()}))
    })
}

fn decode_rows<T: DeserializeOwned>(bytes: &[u8], region: Region) -> Result<Vec<T>> {
    #[derive(Deserialize)]
    struct Rows {
        #[serde(rename = "GET", default)]
        rows: Vec<Value>,
    }
    decode::<Rows>(bytes, "row")?
        .rows
        .into_iter()
        .map(|mut value| {
            let row = value
                .as_object_mut()
                .ok_or_else(|| Error::new(ErrorCode::Protocol, "Klei row must be an object"))?;
            if !row.contains_key("region") {
                row.insert("region".into(), Value::String(region.as_str().into()));
            }
            serde_json::from_value(value)
                .map_err(|_| Error::new(ErrorCode::Protocol, "Klei row does not match its schema"))
        })
        .collect()
}
