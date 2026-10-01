//! A deliberately minimal HTTP/1.1 client: plain `http://` to a LOOPBACK control plane only.
//! No TLS, no redirects, no chunked bodies, no proxies, no URL parser: nothing to misconfigure
//! into talking to anything but the local control plane.
use std::io::{ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};

pub const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq)]
pub enum HttpError {
    BadUrl(String),
    Refused(String),
    Timeout,
    Io(String),
    Protocol(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Response {
    pub status: u16,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Endpoint {
    pub host: String,
    pub port: u16,
}

impl Endpoint {
    /// `http://<127.0.0.1|localhost|[::1]>:<port>` and nothing else.
    pub fn parse(base: &str) -> Result<Endpoint, HttpError> {
        let bad = |why: &str| HttpError::BadUrl(format!("{why}: {base:?}"));
        let rest = base.strip_prefix("http://").ok_or_else(|| bad("must start with http://"))?;
        if rest.is_empty() || rest.contains(['/', '@', '?', '#', ' ']) && !rest.starts_with('[') {
            return Err(bad("must be http://host:port with no path or credentials"));
        }
        let (host, port) = if let Some(inner) = rest.strip_prefix('[') {
            let end = inner.find(']').ok_or_else(|| bad("unterminated IPv6 host"))?;
            let port = inner[end + 1..].strip_prefix(':').ok_or_else(|| bad("a port is required"))?;
            (&inner[..end], port)
        } else {
            rest.rsplit_once(':').ok_or_else(|| bad("a port is required"))?
        };
        if !matches!(host, "127.0.0.1" | "localhost" | "::1") {
            return Err(bad("only loopback hosts are allowed"));
        }
        if port.contains(['/', '@', '?', '#', ' ']) {
            return Err(bad("must be http://host:port with no path or credentials"));
        }
        let port: u16 = port.parse().map_err(|_| bad("invalid port"))?;
        if port == 0 {
            return Err(bad("invalid port"));
        }
        Ok(Endpoint { host: host.to_string(), port })
    }
}

/// One request, one connection (`Connection: close`). `total` bounds the whole exchange after
/// connecting, so a slow-drip server cannot hold the UI hostage.
pub fn request(
    ep: &Endpoint,
    method: &str,
    path: &str,
    connect: Duration,
    total: Duration,
) -> Result<Response, HttpError> {
    request_with(ep, method, path, &[], connect, total)
}

/// As `request`, with extra fixed request headers (e.g. the server's confirmation header).
pub fn request_with(
    ep: &Endpoint,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    connect: Duration,
    total: Duration,
) -> Result<Response, HttpError> {
    request_json(ep, method, path, headers, "", connect, total)
}

/// As `request_with`, carrying a small JSON body (empty for none).
pub fn request_json(
    ep: &Endpoint,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &str,
    connect: Duration,
    total: Duration,
) -> Result<Response, HttpError> {
    if headers.iter().any(|(n, v)| n.contains(['\r', '\n', ':']) || v.contains(['\r', '\n'])) {
        return Err(HttpError::BadUrl("header contains a control character".into()));
    }
    let extra: String = headers.iter().map(|(n, v)| format!("{n}: {v}\r\n")).collect();
    let addrs: Vec<_> = (ep.host.as_str(), ep.port)
        .to_socket_addrs()
        .map_err(|e| HttpError::Io(e.to_string()))?
        .collect();
    let mut last = HttpError::Io("no address".into());
    let mut stream = None;
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, connect) {
            Ok(s) => {
                stream = Some(s);
                break;
            }
            Err(e) => {
                last = match e.kind() {
                    ErrorKind::ConnectionRefused => HttpError::Refused(e.to_string()),
                    ErrorKind::TimedOut | ErrorKind::WouldBlock => HttpError::Timeout,
                    _ => HttpError::Io(e.to_string()),
                }
            }
        }
    }
    let mut stream = stream.ok_or(last)?;
    let deadline = Instant::now() + total;
    let _ = stream.set_write_timeout(Some(total));
    let length = if method == "POST" {
        format!("Content-Length: {}\r\n", body.len())
    } else {
        String::new()
    };
    let kind = if body.is_empty() { "" } else { "Content-Type: application/json\r\n" };
    let head = format!(
        "{method} {path} HTTP/1.1\r\nHost: {}:{}\r\nUser-Agent: aiops-tui/0.1\r\n\
         Accept: application/json\r\nConnection: close\r\n{length}{kind}{extra}\r\n",
        ep.host, ep.port
    );
    stream.write_all(head.as_bytes()).map_err(|e| HttpError::Io(e.to_string()))?;
    stream.write_all(body.as_bytes()).map_err(|e| HttpError::Io(e.to_string()))?;

    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(HttpError::Timeout);
        }
        let _ = stream.set_read_timeout(Some(remaining.min(Duration::from_millis(250))));
        let eof = match stream.read(&mut chunk) {
            Ok(0) => true,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > MAX_RESPONSE_BYTES {
                    return Err(HttpError::Protocol(format!(
                        "response too large (over {MAX_RESPONSE_BYTES} bytes)"
                    )));
                }
                false
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => continue,
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(HttpError::Io(e.to_string())),
        };
        if let Some(response) = parse_response(&buf, eof)? {
            return Ok(response);
        }
        if eof {
            return Err(HttpError::Protocol("connection closed before a complete response".into()));
        }
    }
}

/// `Ok(None)` means "need more bytes". `eof` says no more are coming.
pub fn parse_response(buf: &[u8], eof: bool) -> Result<Option<Response>, HttpError> {
    let Some(split) = buf.windows(4).position(|w| w == b"\r\n\r\n") else {
        return if eof {
            Err(HttpError::Protocol("connection closed before the response headers".into()))
        } else {
            Ok(None)
        };
    };
    let head = String::from_utf8_lossy(&buf[..split]).into_owned();
    let mut lines = head.lines();
    let status_line = lines.next().unwrap_or("");
    let mut parts = status_line.split_whitespace();
    let version = parts.next().unwrap_or("");
    if !version.starts_with("HTTP/1.") {
        return Err(HttpError::Protocol(format!("not an HTTP response: {status_line:?}")));
    }
    let status: u16 = parts
        .next()
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| HttpError::Protocol(format!("bad status line: {status_line:?}")))?;
    let mut length: Option<usize> = None;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else { continue };
        let (name, value) = (name.trim().to_ascii_lowercase(), value.trim());
        if name == "transfer-encoding" && value.to_ascii_lowercase().contains("chunked") {
            return Err(HttpError::Protocol("chunked transfer encoding is not supported".into()));
        }
        if name == "content-length" {
            length = Some(value.parse().map_err(|_| {
                HttpError::Protocol(format!("bad content-length: {value:?}"))
            })?);
        }
    }
    let body = &buf[split + 4..];
    let body = match length {
        Some(n) if body.len() >= n => &body[..n],
        Some(n) if eof => {
            return Err(HttpError::Protocol(format!(
                "truncated body: got {} of {n} bytes",
                body.len()
            )))
        }
        Some(_) => return Ok(None),
        None if eof => body,
        None => return Ok(None),
    };
    let body = String::from_utf8(body.to_vec())
        .map_err(|_| HttpError::Protocol("response body is not valid UTF-8".into()))?;
    Ok(Some(Response { status, body }))
}
