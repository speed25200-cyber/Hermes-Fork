"""Connecteurs de sources publiques (§13, §50.1) : allowlist, fetcher sûr, parseurs bornés, datation explicite.

Sécurité :
- seuls les hôtes et schémas de ``configs/sources.example.yaml`` (ou du fichier configuré) sont joignables ;
- avant TOUTE connexion, l'hôte est résolu (résolveur injectable) et chaque adresse doit être publique :
  plages privées, loopback, lien-local, multicast, réservées, ULA IPv6, IPv4 mappées, métadonnées cloud
  (169.254.169.254, fd00:ec2::254, ...) sont refusées (T56) ;
- une redirection n'est suivie qu'après validation complète de la destination ;
- taille, profondeur (redirections, éléments) et temps sont bornés ;
- XML/RSS/Atom via ``defusedxml`` (entités et DTD refusées) ; HTML via ``html.parser`` borné ;
- un document est une DONNÉE : son contenu n'est jamais interprété comme une instruction (T55).

Datation (T58) : ``published_at`` provient d'une méthode explicite (``page_metadata``, ``declared_field``,
``rss_pubdate``) ; à défaut il vaut ``None`` avec ``date_method="absent"``. L'heure de collecte
(``received_at``) n'est JAMAIS utilisée comme substitut.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
import yaml
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as SafeParseError
from defusedxml.ElementTree import fromstring as safe_fromstring
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from okxq.domain.clocks import Clock, SystemClock, ensure_utc
from okxq.domain.errors import DataQualityError, JevError, SsrfBlockedError
from okxq.domain.events import AssetMapping, SourceDocument
from okxq.jev.schemas import parse_json_strict

if TYPE_CHECKING:  # pragma: no cover - typage seulement
    from xml.etree.ElementTree import Element

    from okxq.jev.entity_mapping import EntityRegistry

log = structlog.get_logger("okxq.jev.sources")

SourceKind = Literal["html", "html_list", "rss", "json"]
DateMethod = Literal["page_metadata", "declared_field", "rss_pubdate", "absent"]
DATE_METHOD_ABSENT: DateMethod = "absent"

DEFAULT_MAX_TAGS = 20000
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_CHARACTERS = 20000
MAX_HTML_BYTES_TO_PARSE = 2 * 1024 * 1024

# Adresses de métadonnées cloud connues (AWS/GCP/Azure, Alibaba, Oracle) : refusées quel que soit le nom.
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("192.0.0.192"),
    }
)

Resolver = Callable[[str], list[str]]


class SourceFetchError(JevError):
    code = "SOURCE_FETCH_FAILED"


class SourceParseError(DataQualityError):
    code = "SOURCE_PARSE_FAILED"


# --- allowlist ------------------------------------------------------------------------------------------


class StrictSourceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class JsonFieldMap(StrictSourceModel):
    """Champs DÉCLARÉS d'une source JSON (``date_method: declared_field``)."""

    items: str | None = None
    title: str = "title"
    text: str = "text"
    url: str | None = "url"
    published_at: str = "published_at"
    guid: str | None = "id"


class SourceSpec(StrictSourceModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    kind: SourceKind
    url: str = Field(min_length=8, max_length=2048)
    language: str | None = Field(default=None, min_length=2, max_length=8)
    date_method: DateMethod
    poll_interval_seconds: int = Field(gt=0)
    enabled: bool = True
    json_fields: JsonFieldMap = JsonFieldMap()

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("url de source : HTTPS avec hôte requis")
        return v

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


class SourceAllowlist(StrictSourceModel):
    version: Literal[1]
    allowed_schemes: list[Literal["https"]] = Field(min_length=1)
    max_document_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    max_redirects: int = Field(ge=0, le=5)
    fetch_timeout_seconds: int = Field(gt=0, le=120)
    sources: list[SourceSpec]

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[SourceSpec]) -> list[SourceSpec]:
        ids = [s.id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("identifiants de sources dupliqués")
        return v

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(s.host for s in self.sources)

    def source(self, source_id: str) -> SourceSpec:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise JevError(f"source inconnue : {source_id}", code="SOURCE_UNKNOWN", source_id=source_id)


def load_allowlist(path: str | Path) -> SourceAllowlist:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise JevError(f"allowlist de sources introuvable : {p}", code="SOURCE_ALLOWLIST_MISSING") from exc
    except yaml.YAMLError as exc:
        raise JevError(f"allowlist de sources illisible : {exc}", code="SOURCE_ALLOWLIST_INVALID") from exc
    try:
        return SourceAllowlist.model_validate(raw)
    except ValidationError as exc:
        raise JevError(f"allowlist de sources invalide : {exc}", code="SOURCE_ALLOWLIST_INVALID") from exc


# --- SSRF -------------------------------------------------------------------------------------------------


def is_public_address(raw: str) -> bool:
    """Vrai seulement pour une adresse globale routable, hors métadonnées cloud."""
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr in METADATA_ADDRESSES:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    if isinstance(addr, ipaddress.IPv6Address) and (
        addr.is_site_local or addr.sixtofour is not None or addr.teredo
    ):
        return False
    return bool(addr.is_global)


def system_resolver(host: str) -> list[str]:
    """Résolution DNS réelle (exploitation seulement ; les tests injectent un résolveur)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlockedError(f"résolution impossible pour {host}", host=host) from exc
    return sorted({str(info[4][0]) for info in infos})


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    url: str
    host: str
    addresses: tuple[str, ...]


def validate_target(url: str, *, allowlist: SourceAllowlist, resolver: Resolver) -> ValidatedTarget:
    """Valide une destination AVANT connexion : schéma, hôte allowlisté, résolution, adresses publiques."""
    parts = urlsplit(url)
    if parts.scheme not in allowlist.allowed_schemes:
        raise SsrfBlockedError(f"schéma refusé : {parts.scheme or 'absent'}", url=url)
    if parts.username or parts.password:
        raise SsrfBlockedError("identifiants dans l'URL refusés", url=url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SsrfBlockedError("hôte absent", url=url)
    if parts.port not in (None, 443):
        raise SsrfBlockedError(f"port non standard refusé : {parts.port}", url=url)
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise SsrfBlockedError(
            "adresse IP littérale refusée : seuls les hôtes allowlistés sont joignables", url=url
        )
    if host not in allowlist.hosts:
        raise SsrfBlockedError(f"hôte hors allowlist : {host}", url=url, host=host)
    addresses = tuple(resolver(host))
    if not addresses:
        raise SsrfBlockedError(f"aucune adresse pour {host}", url=url, host=host)
    for address in addresses:
        if not is_public_address(address):
            raise SsrfBlockedError(
                f"{host} résout vers une adresse non publique ({address}) : connexion refusée",
                url=url,
                host=host,
                address=address,
            )
    return ValidatedTarget(url=url, host=host, addresses=addresses)


# --- fetcher --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    fetched_at: datetime
    redirects: tuple[str, ...]


class SafeFetcher:
    """GET HTTPS borné en taille, redirections et temps ; SSRF bloqué avant connexion."""

    def __init__(
        self,
        allowlist: SourceAllowlist,
        *,
        resolver: Resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._allowlist = allowlist
        self._resolver = resolver
        self._transport = transport
        self._clock: Clock = clock or SystemClock()

    async def fetch(self, url: str) -> FetchedPage:
        limits = self._allowlist
        redirects: list[str] = []
        current = url
        try:
            async with asyncio.timeout(limits.fetch_timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        limits.fetch_timeout_seconds, connect=min(5.0, limits.fetch_timeout_seconds)
                    ),
                    headers={"User-Agent": "okx-quant-jev/0.1 (source-collector)", "Accept": "*/*"},
                ) as http:
                    for _hop in range(limits.max_redirects + 1):
                        target = validate_target(current, allowlist=limits, resolver=self._resolver)
                        async with http.stream("GET", target.url) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                location = response.headers.get("Location")
                                if not location:
                                    raise SourceFetchError("redirection sans Location", url=current)
                                redirects.append(current)
                                current = urljoin(current, location)
                                continue
                            if response.status_code != 200:
                                raise SourceFetchError(
                                    f"statut HTTP {response.status_code}",
                                    url=current,
                                    status=response.status_code,
                                )
                            declared = response.headers.get("Content-Length")
                            if declared and declared.isdigit() and int(declared) > limits.max_document_bytes:
                                raise SourceFetchError(
                                    "document trop volumineux (Content-Length)", url=current
                                )
                            body = await _read_bounded(response, limits.max_document_bytes, current)
                            return FetchedPage(
                                url=url,
                                final_url=current,
                                status_code=response.status_code,
                                content_type=response.headers.get("Content-Type", ""),
                                body=body,
                                fetched_at=self._clock.now_utc(),
                                redirects=tuple(redirects),
                            )
                    raise SourceFetchError("trop de redirections", url=url, redirects=redirects)
        except TimeoutError as exc:
            raise SourceFetchError("délai de collecte dépassé", url=url) from exc
        except httpx.HTTPError as exc:
            raise SourceFetchError(f"erreur HTTP : {type(exc).__name__}", url=url) from exc


async def _read_bounded(response: httpx.Response, max_bytes: int, url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise SourceFetchError("document trop volumineux (flux)", url=url, max_bytes=max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


# --- datation explicite --------------------------------------------------------------------------------


def parse_explicit_datetime(raw: object) -> datetime | None:
    """Date déclarée → UTC aware ; ``None`` si absente, naïve ou illisible. Jamais l'heure courante."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
        if value <= 0:
            return None
        if value > 1e11:  # millisecondes epoch
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        iso = None
    if iso is not None:
        return None if iso.tzinfo is None else ensure_utc(iso)
    try:
        rfc = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if rfc is None or rfc.tzinfo is None:
        return None
    return ensure_utc(rfc)


# --- parseurs bornés -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedItem:
    title: str
    text: str
    url: str | None
    published_at: datetime | None
    date_method: DateMethod
    guid: str | None = None
    language: str | None = None
    truncated: bool = False


class _StopParsing(Exception):
    pass


_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}
)
_META_DATE_KEYS = frozenset(
    {
        "article:published_time",
        "og:published_time",
        "datepublished",
        "date",
        "pubdate",
        "publishdate",
        "dc.date",
    }
)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self, *, max_characters: int, max_tags: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_characters = max_characters
        self._max_tags = max_tags
        self._tags = 0
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.chars = 0
        self.truncated = False
        self.date_candidates: list[str] = []
        self.lang: str | None = None
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tags += 1
        if self._tags > self._max_tags:
            self.truncated = True
            raise _StopParsing
        attributes = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and attributes.get("lang"):
            self.lang = attributes["lang"][:8]
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in _META_DATE_KEYS and attributes.get("content"):
                self.date_candidates.append(attributes["content"])
        elif tag == "time" and attributes.get("datetime"):
            self.date_candidates.append(attributes["datetime"])
        elif tag == "a" and attributes.get("href"):
            self._current_href = attributes["href"]
            self._anchor_parts = []
        if tag in _BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._current_href is not None:
            self.links.append((self._current_href, " ".join(self._anchor_parts).strip()))
            self._current_href = None
        if tag in _BLOCK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._current_href is not None:
            self._anchor_parts.append(data.strip())
        self._append(data)

    def _append(self, data: str) -> None:
        if not data:
            return
        remaining = self._max_characters - self.chars
        if remaining <= 0:
            self.truncated = True
            raise _StopParsing
        piece = data[:remaining]
        self.text_parts.append(piece)
        self.chars += len(piece)
        if len(piece) < len(data):
            self.truncated = True
            raise _StopParsing


_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def normalize_text(text: str) -> str:
    """Espaces et lignes vides compactés ; caractères de contrôle retirés ; contenu inchangé sinon."""
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    cleaned = _WS.sub(" ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    return _NL.sub("\n\n", cleaned).strip()


def parse_html_document(
    body: bytes,
    *,
    date_method: DateMethod,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
    max_tags: int = DEFAULT_MAX_TAGS,
) -> ParsedItem:
    """Texte + titre d'une page HTML ; date via métadonnées de page si ``date_method="page_metadata"``."""
    parser = _HtmlTextExtractor(max_characters=max_characters, max_tags=max_tags)
    text = body[:MAX_HTML_BYTES_TO_PARSE].decode("utf-8", errors="replace")
    try:
        parser.feed(text)
        parser.close()
    except _StopParsing:
        pass
    published_at: datetime | None = None
    method: DateMethod = DATE_METHOD_ABSENT
    if date_method == "page_metadata":
        for candidate in parser.date_candidates:
            published_at = parse_explicit_datetime(candidate)
            if published_at is not None:
                method = "page_metadata"
                break
    return ParsedItem(
        title=normalize_text("".join(parser.title_parts))[:512],
        text=normalize_text("".join(parser.text_parts)),
        url=None,
        published_at=published_at,
        date_method=method,
        language=parser.lang,
        truncated=parser.truncated or len(body) > MAX_HTML_BYTES_TO_PARSE,
    )


def extract_links(
    body: bytes, *, base_url: str, max_links: int = DEFAULT_MAX_ITEMS, max_tags: int = DEFAULT_MAX_TAGS
) -> list[tuple[str, str]]:
    """Liens absolus (href, texte) d'une page de liste, bornés et limités au même hôte."""
    parser = _HtmlTextExtractor(max_characters=DEFAULT_MAX_CHARACTERS, max_tags=max_tags)
    try:
        parser.feed(body[:MAX_HTML_BYTES_TO_PARSE].decode("utf-8", errors="replace"))
        parser.close()
    except _StopParsing:
        pass
    base_host = (urlsplit(base_url).hostname or "").lower()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, label in parser.links:
        absolute = urljoin(base_url, href.strip())
        parts = urlsplit(absolute)
        if parts.scheme != "https" or (parts.hostname or "").lower() != base_host or absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, label))
        if len(out) >= max_links:
            break
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: Element, *names: str) -> str | None:
    for child in node:
        if _local(child.tag) in names:
            if _local(child.tag) == "link" and child.get("href"):
                return child.get("href")
            return (child.text or "").strip() or None
    return None


def parse_feed(
    body: bytes,
    *,
    date_method: DateMethod,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
) -> list[ParsedItem]:
    """RSS 2.0 / Atom via defusedxml (DTD, entités internes et externes refusées)."""
    try:
        root = safe_fromstring(body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise SourceParseError(f"XML refusé : {type(exc).__name__}") from exc
    except SafeParseError as exc:
        raise SourceParseError(f"XML invalide : {exc}") from exc
    items: list[ParsedItem] = []
    nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    for node in nodes[:max_items]:
        title = _child_text(node, "title") or ""
        raw_text = _child_text(node, "content", "description", "summary", "encoded") or ""
        text, truncated = _bounded(strip_markup(raw_text), max_characters)
        published_at: datetime | None = None
        method: DateMethod = DATE_METHOD_ABSENT
        if date_method == "rss_pubdate":
            declared = _child_text(node, "pubdate", "published", "updated", "date")
            published_at = parse_explicit_datetime(declared)
            if published_at is not None:
                method = "rss_pubdate"
        link = _child_text(node, "link")
        guid = _child_text(node, "guid", "id")
        items.append(
            ParsedItem(
                title=normalize_text(title)[:512],
                text=normalize_text(text),
                url=link,
                published_at=published_at,
                date_method=method,
                guid=guid,
                truncated=truncated,
            )
        )
    return items


def strip_markup(text: str) -> str:
    """Retire les balises HTML d'un fragment (descriptions RSS) avec le parseur borné."""
    if "<" not in text:
        return text
    parser = _HtmlTextExtractor(max_characters=DEFAULT_MAX_CHARACTERS, max_tags=DEFAULT_MAX_TAGS)
    try:
        parser.feed(text)
        parser.close()
    except _StopParsing:
        pass
    return "".join(parser.text_parts)


def _bounded(text: str, max_characters: int) -> tuple[str, bool]:
    if len(text) <= max_characters:
        return text, False
    return text[:max_characters], True


def _dig(node: Any, path: str | None) -> Any:
    if not path:
        return node
    current = node
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def parse_json_items(
    body: bytes,
    *,
    fields: JsonFieldMap,
    date_method: DateMethod,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
) -> list[ParsedItem]:
    """Éléments d'un document JSON selon des champs DÉCLARÉS ; la date vient du champ déclaré ou est absente."""
    try:
        data = parse_json_strict(body)
    except JevError as exc:
        raise SourceParseError(f"JSON invalide : {exc}") from exc
    container = _dig(data, fields.items)
    if isinstance(container, Mapping):
        container = [container]
    if not isinstance(container, list):
        raise SourceParseError("JSON : liste d'éléments introuvable")
    items: list[ParsedItem] = []
    for entry in container[:max_items]:
        if not isinstance(entry, Mapping):
            continue
        title = str(_dig(entry, fields.title) or "")
        raw_text = _dig(entry, fields.text)
        text, truncated = _bounded(str(raw_text) if raw_text is not None else "", max_characters)
        published_at: datetime | None = None
        method: DateMethod = DATE_METHOD_ABSENT
        if date_method == "declared_field":
            published_at = parse_explicit_datetime(_dig(entry, fields.published_at))
            if published_at is not None:
                method = "declared_field"
        url = _dig(entry, fields.url) if fields.url else None
        guid = _dig(entry, fields.guid) if fields.guid else None
        items.append(
            ParsedItem(
                title=normalize_text(title)[:512],
                text=normalize_text(text),
                url=str(url) if url else None,
                published_at=published_at,
                date_method=method,
                guid=str(guid) if guid is not None else None,
                truncated=truncated,
            )
        )
    return items


# --- construction des documents ----------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deduplication_id(source_id: str, item: ParsedItem) -> str:
    """Identité logique d'un document : source + guid, sinon URL, sinon titre+date. Le contenu n'y entre pas."""
    key = (
        item.guid or item.url or f"{item.title}|{item.published_at.isoformat() if item.published_at else ''}"
    )
    return hashlib.sha256(f"{source_id}|{key}".encode()).hexdigest()[:40]


def document_id_for(source_id: str, dedup_id: str) -> str:
    return "doc_" + hashlib.sha256(f"{source_id}:{dedup_id}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DocumentCandidate:
    """Document parsé, avant attribution de version par le dépôt."""

    source: str
    source_url: str
    deduplication_id: str
    title: str
    text: str
    raw_text_hash: str
    published_at: datetime | None
    date_method: str
    language: str | None
    received_at: datetime
    parsed_at: datetime
    asset_mapping: tuple[AssetMapping, ...] = ()
    mapping_quality: str | None = None
    asset_mapping_version: str = "none"
    truncated: bool = False

    @property
    def document_id(self) -> str:
        return document_id_for(self.source, self.deduplication_id)


def candidate_from_item(
    source: SourceSpec,
    item: ParsedItem,
    *,
    source_url: str,
    received_at: datetime,
    parsed_at: datetime,
) -> DocumentCandidate:
    if parsed_at < received_at:
        raise JevError("parsed_at < received_at", code="TIMESTAMP_INVALID")
    text = item.text
    if not text and not item.title:
        raise SourceParseError("document vide", source=source.id)
    return DocumentCandidate(
        source=source.id,
        source_url=(item.url or source_url)[:2048],
        deduplication_id=deduplication_id(source.id, item),
        title=item.title,
        text=text,
        raw_text_hash=sha256_text(item.title + "\n" + text),
        published_at=item.published_at,
        date_method=item.date_method,
        language=(item.language or source.language or None),
        received_at=received_at,
        parsed_at=parsed_at,
        truncated=item.truncated,
    )


class DocumentSink(Protocol):
    """Dépôt de documents versionnés : un contenu modifié crée une nouvelle version."""

    def upsert(self, candidate: DocumentCandidate) -> tuple[SourceDocument, bool]: ...


class MemoryDocumentSink:
    """Dépôt en mémoire (tests, outils hors ligne) ; même sémantique de versions que ``DocumentStore``."""

    def __init__(self) -> None:
        self._latest: dict[tuple[str, str], SourceDocument] = {}
        self.history: list[SourceDocument] = []

    def upsert(self, candidate: DocumentCandidate) -> tuple[SourceDocument, bool]:
        key = (candidate.source, candidate.deduplication_id)
        previous = self._latest.get(key)
        if previous is not None and previous.raw_text_hash == candidate.raw_text_hash:
            return previous, False
        version = 1 if previous is None else previous.version + 1
        first_seen_at = candidate.received_at if previous is None else previous.first_seen_at
        document = build_source_document(candidate, version=version, first_seen_at=first_seen_at)
        self._latest[key] = document
        self.history.append(document)
        return document, True

    def all_documents(self) -> list[SourceDocument]:
        return list(self.history)


def build_source_document(
    candidate: DocumentCandidate, *, version: int, first_seen_at: datetime
) -> SourceDocument:
    return SourceDocument(
        document_id=candidate.document_id,
        source=candidate.source,
        source_url=candidate.source_url,
        published_at=candidate.published_at,
        first_seen_at=first_seen_at,
        received_at=candidate.received_at,
        parsed_at=candidate.parsed_at,
        language=candidate.language,
        version=version,
        date_method=candidate.date_method,
        raw_text_hash=candidate.raw_text_hash,
        deduplication_id=candidate.deduplication_id,
        asset_mapping=list(candidate.asset_mapping),
        mapping_quality=candidate.mapping_quality,
        title=candidate.title,
        text=candidate.text,
    )


# --- collecteur -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class CollectResult:
    source_id: str
    documents: list[SourceDocument] = field(default_factory=list)
    new_or_changed: list[SourceDocument] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)


class SourceCollector:
    """Interroge une source, parse, mappe les entités et versionne les documents. Jamais d'exécution du contenu."""

    def __init__(
        self,
        allowlist: SourceAllowlist,
        *,
        fetcher: SafeFetcher,
        sink: DocumentSink,
        registry: EntityRegistry | None = None,
        clock: Clock | None = None,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        self._allowlist = allowlist
        self._fetcher = fetcher
        self._sink = sink
        self._registry = registry
        self._clock: Clock = clock or SystemClock()
        self._max_characters = max_characters
        self._max_items = max_items

    async def poll(self, source_id: str) -> CollectResult:
        source = self._allowlist.source(source_id)
        result = CollectResult(source_id=source_id)
        if not source.enabled:
            result.errors.append("source désactivée")
            return result
        try:
            page = await self._fetcher.fetch(source.url)
        except (SsrfBlockedError, SourceFetchError) as exc:
            result.errors.append(f"{exc.code}: {exc}")
            log.warning("source_fetch_failed", source_id=source_id, code=exc.code)
            return result
        result.fetched_urls.append(page.final_url)
        received_at = page.fetched_at
        try:
            items = await self._items(source, page, result)
        except SourceParseError as exc:
            result.errors.append(f"{exc.code}: {exc}")
            return result
        for item in items:
            try:
                candidate = candidate_from_item(
                    source,
                    item,
                    source_url=page.final_url,
                    received_at=received_at,
                    parsed_at=self._clock.now_utc(),
                )
            except (SourceParseError, JevError) as exc:
                result.errors.append(f"{exc.code}: {exc}")
                continue
            candidate = self._map_entities(candidate)
            document, changed = self._sink.upsert(candidate)
            result.documents.append(document)
            if changed:
                result.new_or_changed.append(document)
        return result

    async def _items(self, source: SourceSpec, page: FetchedPage, result: CollectResult) -> list[ParsedItem]:
        if source.kind == "rss":
            return parse_feed(
                page.body,
                date_method=source.date_method,
                max_items=self._max_items,
                max_characters=self._max_characters,
            )
        if source.kind == "json":
            return parse_json_items(
                page.body,
                fields=source.json_fields,
                date_method=source.date_method,
                max_items=self._max_items,
                max_characters=self._max_characters,
            )
        if source.kind == "html":
            return [
                parse_html_document(
                    page.body, date_method=source.date_method, max_characters=self._max_characters
                )
            ]
        # html_list : profondeur 1 — la page de liste, puis chaque page liée (même hôte), bornées.
        items: list[ParsedItem] = []
        for href, label in extract_links(page.body, base_url=page.final_url, max_links=self._max_items):
            try:
                sub = await self._fetcher.fetch(href)
            except (SsrfBlockedError, SourceFetchError) as exc:
                result.errors.append(f"{exc.code}: {href}")
                continue
            result.fetched_urls.append(sub.final_url)
            parsed = parse_html_document(
                sub.body, date_method=source.date_method, max_characters=self._max_characters
            )
            items.append(
                ParsedItem(
                    title=parsed.title or label,
                    text=parsed.text,
                    url=sub.final_url,
                    published_at=parsed.published_at,
                    date_method=parsed.date_method,
                    guid=None,
                    language=parsed.language,
                    truncated=parsed.truncated,
                )
            )
        return items

    def _map_entities(self, candidate: DocumentCandidate) -> DocumentCandidate:
        if self._registry is None:
            return candidate
        mapping = self._registry.resolve(
            title=candidate.title, text=candidate.text, as_of=candidate.received_at
        )
        return replace(
            candidate,
            asset_mapping=tuple(mapping.mappings),
            mapping_quality=mapping.quality.value,
            asset_mapping_version=mapping.mapping_version,
        )


def collect_hosts(sources: Iterable[SourceSpec]) -> frozenset[str]:
    return frozenset(s.host for s in sources)
