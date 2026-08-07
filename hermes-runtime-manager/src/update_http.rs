use crate::update_download::{
    ArtifactDownloadSpecV1, ArtifactRangeSource, DownloadChunkV1, UpdateDownloadError,
};
use reqwest::blocking::{Client, Request};
use reqwest::header::{ACCEPT_ENCODING, CONTENT_LENGTH, CONTENT_RANGE, RANGE};
use reqwest::{redirect::Policy, StatusCode, Url};
use serde::{Deserialize, Serialize};
use std::io::Read;
use std::time::Duration as StdDuration;
use time::{format_description::well_known::Rfc3339, Duration, OffsetDateTime};

const MAX_GRANT_URL_BYTES: usize = 4096;
const MAX_GRANT_LIFETIME: Duration = Duration::minutes(20);
const MAX_CHUNK_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HttpDownloadGrantV1 {
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub url: String,
    pub expires_at: String,
}

pub struct HttpRangeSource {
    client: Client,
    url: Url,
    total_size: u64,
    expires_at: OffsetDateTime,
}

impl HttpRangeSource {
    pub fn from_grant(
        spec: &ArtifactDownloadSpecV1,
        grant: &HttpDownloadGrantV1,
    ) -> Result<Self, UpdateDownloadError> {
        Self::from_grant_at(spec, grant, OffsetDateTime::now_utc())
    }

    pub fn from_grant_at(
        spec: &ArtifactDownloadSpecV1,
        grant: &HttpDownloadGrantV1,
        now: OffsetDateTime,
    ) -> Result<Self, UpdateDownloadError> {
        if grant.object_key != spec.object_key
            || grant.sha256 != spec.sha256
            || grant.size_bytes != spec.size_bytes
        {
            return Err(UpdateDownloadError::InvalidArtifact(
                "download grant identity does not match signed artifact".to_owned(),
            ));
        }
        if grant.url.is_empty() || grant.url.len() > MAX_GRANT_URL_BYTES {
            return Err(UpdateDownloadError::InvalidArtifact(
                "download grant URL is invalid".to_owned(),
            ));
        }
        let url = Url::parse(&grant.url).map_err(|_| {
            UpdateDownloadError::InvalidArtifact("download grant URL is invalid".to_owned())
        })?;
        if url.scheme() != "https"
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(UpdateDownloadError::InvalidArtifact(
                "download grant must be a credential-free HTTPS URL without fragment".to_owned(),
            ));
        }
        let expires_at = OffsetDateTime::parse(&grant.expires_at, &Rfc3339).map_err(|_| {
            UpdateDownloadError::InvalidArtifact("download grant expiry is invalid".to_owned())
        })?;
        if expires_at <= now || expires_at > now + MAX_GRANT_LIFETIME {
            return Err(UpdateDownloadError::InvalidArtifact(
                "download grant expiry is stale or too long-lived".to_owned(),
            ));
        }
        let client = Client::builder()
            .redirect(Policy::none())
            .connect_timeout(StdDuration::from_secs(10))
            .timeout(StdDuration::from_secs(60))
            .user_agent("Hermes-Runtime-Manager/0.1")
            .build()
            .map_err(|_| UpdateDownloadError::Transport("HTTPS client initialization failed".to_owned()))?;
        Ok(Self {
            client,
            url,
            total_size: spec.size_bytes,
            expires_at,
        })
    }

    fn build_request(
        &self,
        start: u64,
        maximum_bytes: usize,
    ) -> Result<Request, UpdateDownloadError> {
        if maximum_bytes == 0 || maximum_bytes > MAX_CHUNK_BYTES || start >= self.total_size {
            return Err(UpdateDownloadError::InvalidRange(
                "HTTP range request is outside the signed artifact bounds".to_owned(),
            ));
        }
        if OffsetDateTime::now_utc() >= self.expires_at {
            return Err(UpdateDownloadError::Transport(
                "download grant expired before range request".to_owned(),
            ));
        }
        let maximum_end = start
            .checked_add(maximum_bytes as u64 - 1)
            .ok_or_else(|| UpdateDownloadError::InvalidRange("HTTP range overflow".to_owned()))?;
        let end = maximum_end.min(self.total_size - 1);
        self.client
            .get(self.url.clone())
            .header(RANGE, format!("bytes={start}-{end}"))
            .header(ACCEPT_ENCODING, "identity")
            .build()
            .map_err(|_| UpdateDownloadError::Transport("HTTPS range request build failed".to_owned()))
    }
}

impl ArtifactRangeSource for HttpRangeSource {
    fn read_range(
        &mut self,
        start: u64,
        maximum_bytes: usize,
    ) -> Result<DownloadChunkV1, UpdateDownloadError> {
        let request = self.build_request(start, maximum_bytes)?;
        let mut response = self
            .client
            .execute(request)
            .map_err(|_| UpdateDownloadError::Transport("HTTPS range request failed".to_owned()))?;
        if response.status() != StatusCode::PARTIAL_CONTENT {
            return Err(UpdateDownloadError::Transport(
                "HTTPS source did not return 206 Partial Content".to_owned(),
            ));
        }
        let content_range = response
            .headers()
            .get(CONTENT_RANGE)
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| {
                UpdateDownloadError::InvalidRange("HTTP Content-Range is missing".to_owned())
            })?;
        let parsed = parse_content_range(content_range)?;
        if parsed.start != start || parsed.total != self.total_size {
            return Err(UpdateDownloadError::InvalidRange(
                "HTTP Content-Range does not match requested signed artifact".to_owned(),
            ));
        }
        let span = parsed.end - parsed.start + 1;
        if span == 0 || span > maximum_bytes as u64 {
            return Err(UpdateDownloadError::InvalidRange(
                "HTTP Content-Range span exceeds requested range".to_owned(),
            ));
        }
        if let Some(content_length) = response.headers().get(CONTENT_LENGTH) {
            let observed = content_length
                .to_str()
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .ok_or_else(|| {
                    UpdateDownloadError::InvalidRange("HTTP Content-Length is invalid".to_owned())
                })?;
            if observed != span {
                return Err(UpdateDownloadError::InvalidRange(
                    "HTTP Content-Length does not match Content-Range".to_owned(),
                ));
            }
        }
        let mut bytes = Vec::with_capacity(span as usize);
        response
            .take(span + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| UpdateDownloadError::Io(error))?;
        if bytes.len() as u64 != span {
            return Err(UpdateDownloadError::PrematureEof);
        }
        Ok(DownloadChunkV1 {
            start,
            total_size: self.total_size,
            bytes,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ParsedContentRange {
    start: u64,
    end: u64,
    total: u64,
}

fn parse_content_range(value: &str) -> Result<ParsedContentRange, UpdateDownloadError> {
    let rest = value.strip_prefix("bytes ").ok_or_else(|| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range unit is invalid".to_owned())
    })?;
    let (range, total) = rest.split_once('/').ok_or_else(|| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range format is invalid".to_owned())
    })?;
    let (start, end) = range.split_once('-').ok_or_else(|| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range bounds are invalid".to_owned())
    })?;
    let start = start.parse::<u64>().map_err(|_| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range start is invalid".to_owned())
    })?;
    let end = end.parse::<u64>().map_err(|_| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range end is invalid".to_owned())
    })?;
    let total = total.parse::<u64>().map_err(|_| {
        UpdateDownloadError::InvalidRange("HTTP Content-Range total is invalid".to_owned())
    })?;
    if total == 0 || end < start || end >= total {
        return Err(UpdateDownloadError::InvalidRange(
            "HTTP Content-Range values are inconsistent".to_owned(),
        ));
    }
    Ok(ParsedContentRange { start, end, total })
}

#[cfg(test)]
mod tests {
    use super::{parse_content_range, HttpDownloadGrantV1, HttpRangeSource};
    use crate::update_download::{ArtifactDownloadSpecV1, ReleaseArtifactKindV1};
    use time::{Duration, OffsetDateTime};

    fn spec() -> ArtifactDownloadSpecV1 {
        ArtifactDownloadSpecV1 {
            schema_version: 1,
            release_id: "1.0.1+20260807.1.gabcdef12".to_owned(),
            release_generation: 101,
            target: "windows-x86_64".to_owned(),
            kind: ReleaseArtifactKindV1::ManagedReleasePayload,
            object_key: format!(
                "artifacts/v1/sha256/aa/{}/payload.bin",
                "a".repeat(64)
            ),
            file_name: "payload.bin".to_owned(),
            sha256: "a".repeat(64),
            size_bytes: 8192,
            platform_signature: None,
        }
    }

    #[test]
    fn grant_must_match_signed_identity_and_be_short_lived_https() {
        let now = OffsetDateTime::from_unix_timestamp(1_786_121_600).unwrap();
        let expected = spec();
        let grant = HttpDownloadGrantV1 {
            object_key: expected.object_key.clone(),
            sha256: expected.sha256.clone(),
            size_bytes: expected.size_bytes,
            url: "https://updates.example.test/payload.bin?token=short".to_owned(),
            expires_at: (now + Duration::minutes(10))
                .format(&time::format_description::well_known::Rfc3339)
                .unwrap(),
        };
        assert!(HttpRangeSource::from_grant_at(&expected, &grant, now).is_ok());

        let mut wrong = grant.clone();
        wrong.sha256 = "b".repeat(64);
        assert!(HttpRangeSource::from_grant_at(&expected, &wrong, now).is_err());

        let mut insecure = grant.clone();
        insecure.url = "http://updates.example.test/payload.bin".to_owned();
        assert!(HttpRangeSource::from_grant_at(&expected, &insecure, now).is_err());

        let mut too_long = grant;
        too_long.expires_at = (now + Duration::minutes(21))
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap();
        assert!(HttpRangeSource::from_grant_at(&expected, &too_long, now).is_err());
    }

    #[test]
    fn request_is_range_bounded_and_disables_content_encoding() {
        let now = OffsetDateTime::now_utc();
        let expected = spec();
        let grant = HttpDownloadGrantV1 {
            object_key: expected.object_key.clone(),
            sha256: expected.sha256.clone(),
            size_bytes: expected.size_bytes,
            url: "https://updates.example.test/payload.bin?token=short".to_owned(),
            expires_at: (now + Duration::minutes(10))
                .format(&time::format_description::well_known::Rfc3339)
                .unwrap(),
        };
        let source = HttpRangeSource::from_grant_at(&expected, &grant, now).unwrap();
        let request = source.build_request(4096, 4096).unwrap();
        assert_eq!(request.headers()["range"], "bytes=4096-8191");
        assert_eq!(request.headers()["accept-encoding"], "identity");
        assert_eq!(request.url().scheme(), "https");
    }

    #[test]
    fn content_range_parser_is_strict() {
        let parsed = parse_content_range("bytes 4096-8191/16384").unwrap();
        assert_eq!(parsed.start, 4096);
        assert_eq!(parsed.end, 8191);
        assert_eq!(parsed.total, 16384);
        assert!(parse_content_range("bytes */16384").is_err());
        assert!(parse_content_range("items 0-1/2").is_err());
        assert!(parse_content_range("bytes 5-4/10").is_err());
        assert!(parse_content_range("bytes 0-10/10").is_err());
    }
}
