#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
Download books (text, EPUB, Kindle, HTML, audio, ...) from Project Gutenberg.

Choose books by author, by title, by Gutenberg ID, by category, or any
combination. Each selector can be given several times (or as several
positional authors); different selectors narrow each other (AND), several
values of the same selector add up (OR).

Book lookup uses Project Gutenberg's official catalog (pg_catalog.csv), which is
downloaded once and cached (or pass your own with --catalog). If the catalog is
unreachable, the Gutendex API (text items only) is used as a fallback.

Usage examples:
    uv run gutenberg_author_download.py "Dickens, Charles"
    uv run gutenberg_author_download.py "Jane Austen" "Mark Twain" --lang en
    uv run gutenberg_author_download.py --id 5148,1342 --format epub
    uv run gutenberg_author_download.py --id 100-120 --format txt
    uv run gutenberg_author_download.py --title "Pride and Prejudice" --title "Emma"
    uv run gutenberg_author_download.py --category "Detective" --lang en --limit 20
    uv run gutenberg_author_download.py "Doyle, Arthur Conan" --format audio
    uv run gutenberg_author_download.py "Doyle, Arthur Conan" --format epub,audio
    uv run gutenberg_author_download.py --id 5148 --format epub,txt --first-available
    uv run gutenberg_author_download.py --id 5148 --format all
    uv run gutenberg_author_download.py --list-categories science
"""

from __future__ import annotations

import argparse
import csv
import functools
import html
import json
import logging
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlparse

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE = "https://www.gutenberg.org"
API_URL = "https://gutendex.com/books/"
CATALOG_URL = f"{SITE}/cache/epub/feeds/pg_catalog.csv"
CATALOG_CACHE = Path.home() / ".cache" / "gutenberg" / "pg_catalog.csv"
CATALOG_MAX_AGE = 7 * 24 * 3600  # seconds
CATALOG_STALE_WARN = 30 * 24 * 3600  # warn if a local file is older than this
ROBOT_DELAY = 2.0  # seconds, per Project Gutenberg's robot access policy
TRANSFER_ATTEMPTS = 3  # tries per URL when a transfer breaks off mid-way

RETRY_STATUS = {429, 500, 502, 503, 504}

# Formats whose URL can be built from the book ID. "{site}" and "{id}" are
# filled in at download time; candidates are tried in order.
PATTERN_FORMATS: dict[str, tuple[str, tuple[str, ...]]] = {
    "txt": (".txt", (
        "{site}/ebooks/{id}.txt.utf-8",
        "{site}/cache/epub/{id}/pg{id}.txt",
        "{site}/files/{id}/{id}-0.txt",
        "{site}/files/{id}/{id}.txt",
    )),
    "epub": (".epub", (
        "{site}/ebooks/{id}.epub3.images",
        "{site}/ebooks/{id}.epub.images",
        "{site}/ebooks/{id}.epub.noimages",
        "{site}/cache/epub/{id}/pg{id}.epub",
    )),
    "html": (".html", (
        "{site}/cache/epub/{id}/pg{id}-images.html",
        "{site}/files/{id}/{id}-h/{id}-h.htm",
    )),
    "htmlzip": (".zip", (
        "{site}/cache/epub/{id}/pg{id}-h.zip",
        "{site}/files/{id}/{id}-h.zip",
    )),
    "kindle": (".mobi", (
        "{site}/ebooks/{id}.kf8.images",
        "{site}/ebooks/{id}.kindle.images",
        "{site}/ebooks/{id}.kindle.noimages",
    )),
}

# File extensions that count as audio. Audiobooks are usually offered in several
# encodings of the same recording (mp3, ogg, m4b, spx ...); "audio" picks one,
# in this order of preference.
AUDIO_PREFERENCE = ("mp3", "ogg", "oga", "m4b", "m4a", "opus", "spx", "wav", "flac")
AUDIO_EXTS = set(AUDIO_PREFERENCE)

# Spellings accepted in --format.
FORMAT_ALIASES = {
    "text": "txt",
    "mobi": "kindle",
    "azw3": "kindle",
    "sound": "audio",
    "audiobook": "audio",
    "everything": "all",
}
ANY_ORDER = ("epub", "txt", "html", "kindle", "audio")  # what "any" expands to

# Local file extension for the "/ebooks/{id}.<family>..." links on a book page.
EBOOK_LINK_EXT = {
    "epub": ".epub",
    "epub3": ".epub",
    "kf8": ".mobi",
    "kindle": ".mobi",
    "txt": ".txt",
    "html": ".html",
}

# Catalog "Type" values (lower-case) and the nicknames accepted in --type.
KNOWN_TYPES = {"text", "sound", "image", "stillimage", "movingimage", "dataset", "collection"}
TYPE_ALIASES = {"audio": "sound", "video": "movingimage", "images": "image"}

USER_AGENT = (
    "gutenberg-downloader/4.0 "
    "(personal use; +https://www.gutenberg.org/policy/robot_access.html)"
)

logger = logging.getLogger("gutenberg")

# Set on Ctrl+C so worker threads stop promptly.
STOP = threading.Event()


class Cancelled(Exception):
    """Raised inside worker threads when the user interrupts the run."""


class Truncated(requests.exceptions.RequestException):
    """The server closed the connection before sending the whole file."""


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Casefold, drop accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


def safe_filename(text: str, max_bytes: int = 150) -> str:
    """Filesystem-safe name component, limited by UTF-8 byte length."""
    text = re.sub(r"[\s ]+", " ", text)
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip(" .")
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        text = raw[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return text or "untitled"


def split_values(value: str | None) -> list[str]:
    """Split a catalog field such as 'a; b; c'."""
    return [p.strip() for p in (value or "").split(";") if p.strip()]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Book:
    id: str
    title: str
    authors: tuple[str, ...]
    languages: tuple[str, ...] = ()
    type: str = "Text"
    subjects: tuple[str, ...] = ()
    bookshelves: tuple[str, ...] = ()

    def display_title(self) -> str:
        return " ".join(self.title.split()) or f"Untitled {self.id}"


# ---------------------------------------------------------------------------
# Author matching
# ---------------------------------------------------------------------------

_ROLE_RE = re.compile(r"\[[^\]]*\]\s*$")


def has_role(name: str) -> bool:
    """True for catalog names tagged with a role, e.g. '... [Illustrator]'."""
    return bool(_ROLE_RE.search(name))


@functools.lru_cache(maxsize=None)
def _author_words(name: str) -> frozenset[str]:
    """Words of a catalog author name, without role tags and years."""
    base = re.sub(r"\[.*?\]", " ", name)
    base = re.sub(r"\b\d{4}\b", " ", base)
    return frozenset(normalize(base).split())


def query_words(query: str) -> frozenset[str]:
    """Words of an author query, without years."""
    return frozenset(normalize(re.sub(r"\b\d{4}\b", " ", query)).split())


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def parse_ids(values: list[str]) -> list[tuple[int, int]]:
    """Parse '5148', '100-120', '1,2,3' (repeatable) into inclusive ranges."""
    ranges: list[tuple[int, int]] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(?:pg)?(\d+)(?:\s*-\s*(\d+))?", part, re.I)
            if not m:
                raise ValueError(
                    f"bad book id {part!r} (use numbers like 5148 or ranges like 100-120)"
                )
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if hi < lo:
                raise ValueError(f"bad id range {part!r}: the end is below the start")
            ranges.append((lo, hi))
    return ranges


def parse_types(value: str) -> set[str] | None:
    """Parse --type. None means 'any type'."""
    out: set[str] = set()
    for raw in value.split(","):
        t = raw.strip().lower()
        if not t:
            continue
        t = TYPE_ALIASES.get(t, t)
        if t == "any":
            return None
        if t not in KNOWN_TYPES:
            raise ValueError(
                f"unknown type {raw.strip()!r}; choose from: any, "
                + ", ".join(sorted(KNOWN_TYPES | set(TYPE_ALIASES)))
            )
        out.add(t)
    return out


class Selector:
    """Decides which catalog entries are wanted."""

    def __init__(
        self,
        *,
        authors: list[str],
        titles: list[str],
        categories: list[str],
        id_ranges: list[tuple[int, int]],
        langs: set[str] | None,
        types: set[str] | None,
        include_contributors: bool = False,
        sole_author: bool = False,
    ) -> None:
        self.authors = authors
        self.titles = titles
        self.categories = categories
        self.id_ranges = id_ranges
        self.langs = langs
        self.types = types
        self.include_contributors = include_contributors
        self.sole_author = sole_author
        self._author_q = [query_words(a) for a in authors]
        self._title_q = [frozenset(normalize(t).split()) for t in titles]
        self._cat_q = [c.casefold().strip() for c in categories]

    @property
    def has_filters(self) -> bool:
        return bool(self.authors or self.titles or self.categories or self.id_ranges)

    def id_ok(self, book_id: str) -> bool:
        if not self.id_ranges:
            return True
        try:
            n = int(book_id)
        except ValueError:
            return False
        return any(lo <= n <= hi for lo, hi in self.id_ranges)

    def type_ok(self, book_type: str) -> bool:
        return self.types is None or book_type.lower() in self.types

    def author_hit(self, name: str) -> bool:
        """Does this single catalog name match one of the author queries?"""
        words = _author_words(name)
        return any(q and q <= words for q in self._author_q)

    def matches(self, b: Book) -> bool:
        if not self.type_ok(b.type) or not self.id_ok(b.id):
            return False
        if self.langs and not (set(b.languages) & self.langs):
            return False
        if self._title_q:
            words = frozenset(normalize(b.title).split())
            if not any(q and q <= words for q in self._title_q):
                return False
        if self._cat_q:
            hay = [s.casefold() for s in (*b.bookshelves, *b.subjects)]
            if not any(c in h for c in self._cat_q for h in hay):
                return False
        if self._author_q:
            if self.sole_author and len(b.authors) != 1:
                return False
            pool = b.authors if self.include_contributors else [
                n for n in b.authors if not has_role(n)
            ]
            if not any(self.author_hit(n) for n in pool):
                return False
        return True


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


class RateLimiter:
    """
    Thread-safe pacing: successive requests start at least `interval` seconds
    apart, no matter how many worker threads there are.
    """

    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if STOP.is_set():
            raise Cancelled()
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        pause = start - now
        if pause > 0 and STOP.wait(pause):
            raise Cancelled()


def _retry_after(r: requests.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return min(120.0, float(value))
    return None


def describe_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return str(e)


def get_with_retries(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 5,
    stream: bool = False,
    timeout: int = 60,
    limiter: RateLimiter | None = None,
) -> requests.Response:
    """
    GET with exponential backoff.

    Only connection errors, timeouts and 429/5xx responses are retried. Other
    errors (404, 403, ...) raise immediately, because retrying won't help.
    """
    last_err: Exception | None = None
    for i in range(attempts):
        if limiter is not None:
            limiter.wait()
        elif STOP.is_set():
            raise Cancelled()

        retry_after: float | None = None
        try:
            r = session.get(url, params=params, timeout=timeout, stream=stream)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
        else:
            if r.status_code in RETRY_STATUS:
                last_err = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
                retry_after = _retry_after(r)
                r.close()
            else:
                try:
                    r.raise_for_status()
                except requests.HTTPError:
                    r.close()
                    raise
                return r

        if i < attempts - 1:
            wait = retry_after or min(60.0, 3.0 * (2**i))
            logger.warning("Request failed (%s); retrying in %.0fs ...", last_err, wait)
            if STOP.wait(wait):
                raise Cancelled()
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Catalog lookup
# ---------------------------------------------------------------------------

def load_catalog(catalog_path: Path | None = None) -> Path:
    """
    Return the path to the catalog CSV.

    A user-supplied path is used as-is (never downloaded or overwritten).
    Otherwise the cache file is used, and refreshed when missing or old. If a
    refresh fails but an older cached copy exists, that copy is used.
    """
    if catalog_path is not None:
        path = catalog_path.expanduser().resolve()
        if not path.is_file():
            sys.exit(f"Catalog file not found: {path}")
        age = time.time() - path.stat().st_mtime
        if age > CATALOG_STALE_WARN:
            logger.warning(
                "Local catalog is %d days old (%s). Consider re-downloading from %s",
                int(age / 86400), path, CATALOG_URL,
            )
        logger.info("Using local catalog: %s", path)
        return path

    cached = CATALOG_CACHE.exists()
    if cached and time.time() - CATALOG_CACHE.stat().st_mtime < CATALOG_MAX_AGE:
        age = time.time() - CATALOG_CACHE.stat().st_mtime
        logger.info("Using cached catalog: %s (%.0f hours old)", CATALOG_CACHE, age / 3600)
        return CATALOG_CACHE

    logger.info("Downloading Project Gutenberg catalog (cached for one week) ...")
    try:
        r = get_with_retries(CATALOG_URL)
        if b"Text#" not in r.content[:300]:
            raise requests.RequestException("catalog download returned unexpected content")
    except requests.RequestException:
        if cached:
            logger.warning("Could not refresh the catalog; using the older cached copy.")
            return CATALOG_CACHE
        raise
    CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_CACHE.with_name(CATALOG_CACHE.name + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(CATALOG_CACHE)
    return CATALOG_CACHE


def read_catalog_rows(path: Path) -> Iterator[dict[str, str]]:
    try:
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError as e:
        sys.exit(f"Cannot open catalog: {e}")
    with f:
        reader = csv.DictReader(f)
        required = {"Text#", "Type", "Title", "Authors"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            sys.exit(
                f"Catalog does not look like pg_catalog.csv (headers: {reader.fieldnames})"
            )
        yield from reader


def fetch_from_catalog(sel: Selector, catalog_path: Path | None = None) -> list[Book]:
    """Scan the official catalog CSV for matching entries."""
    books: dict[str, Book] = {}
    for row in read_catalog_rows(load_catalog(catalog_path)):
        book_type = row.get("Type") or ""
        book_id = (row.get("Text#") or "").strip()
        if not book_id or not sel.type_ok(book_type) or not sel.id_ok(book_id):
            continue
        b = Book(
            id=book_id,
            title=row.get("Title") or "",
            authors=tuple(split_values(row.get("Authors"))),
            languages=tuple(sorted(x.lower() for x in split_values(row.get("Language")))),
            type=book_type,
            subjects=tuple(split_values(row.get("Subjects"))),
            bookshelves=tuple(split_values(row.get("Bookshelves"))),
        )
        if sel.matches(b):
            books[b.id] = b
    return list(books.values())


def list_categories(
    catalog_path: Path | None, types: set[str] | None, text: str
) -> list[tuple[str, int]]:
    """Count catalog bookshelves (categories), optionally filtered by `text`."""
    counts: Counter[str] = Counter()
    needle = text.casefold().strip()
    for row in read_catalog_rows(load_catalog(catalog_path)):
        if types is not None and (row.get("Type") or "").lower() not in types:
            continue
        for shelf in split_values(row.get("Bookshelves")):
            if not needle or needle in shelf.casefold():
                counts[shelf] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))


# ---------------------------------------------------------------------------
# Gutendex (fallback, text items only)
# ---------------------------------------------------------------------------

def _gutendex_results(params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    url: str | None = API_URL
    page = 0
    while url:
        page += 1
        logger.debug("Gutendex page %d", page)
        data = get_with_retries(url, params=params).json()
        yield from data.get("results", [])
        url = data.get("next")
        params = None  # subsequent pages carry the query in the "next" URL


def fetch_from_gutendex(sel: Selector) -> list[Book]:
    """Query Gutendex for the selection. Text items only."""
    base: dict[str, Any] = {}
    if sel.langs:
        base["languages"] = ",".join(sorted(sel.langs))

    queries: list[dict[str, Any]] = []
    if sel.id_ranges:
        total = sum(hi - lo + 1 for lo, hi in sel.id_ranges)
        if total > 5000:
            sys.exit("Too many ids for Gutendex; use the catalog source instead.")
        ids = [str(n) for lo, hi in sel.id_ranges for n in range(lo, hi + 1)]
        queries = [{"ids": ",".join(ids[i:i + 40])} for i in range(0, len(ids), 40)]
    elif sel.authors:
        queries = [{"search": " ".join(sorted(q, key=str))} for q in sel._author_q]
    elif sel.titles:
        queries = [{"search": normalize(t)} for t in sel.titles]
    elif sel.categories:
        queries = [{"topic": c} for c in sel.categories]

    books: dict[str, Book] = {}
    for q in queries:
        for item in _gutendex_results({**base, **q}):
            b = Book(
                id=str(item["id"]),
                title=item.get("title") or "",
                authors=tuple(a["name"] for a in item.get("authors", [])),
                languages=tuple(item.get("languages") or []),
                type="Text",
                subjects=tuple(item.get("subjects") or []),
                bookshelves=tuple(item.get("bookshelves") or []),
            )
            if sel.matches(b):
                books[b.id] = b
    return list(books.values())


def resolve_books(args: argparse.Namespace, sel: Selector) -> list[Book]:
    source = args.source
    catalog_path = Path(args.catalog) if args.catalog else None
    if catalog_path is not None and source == "auto":
        source = "catalog"

    books: list[Book] | None = None
    if source in ("auto", "catalog"):
        try:
            books = fetch_from_catalog(sel, catalog_path)
        except requests.RequestException as e:
            if source == "catalog":
                sys.exit(f"Could not get the Gutenberg catalog: {e}")
            logger.warning("Catalog unavailable (%s). Falling back to Gutendex.", e)

    if books is None:
        if sel.types is None or "sound" in sel.types:
            logger.warning("Gutendex only knows text items; audio items will not be found.")
        try:
            books = fetch_from_gutendex(sel)
        except requests.RequestException as e:
            sys.exit(f"Gutendex unavailable: {e}")

    def sort_key(b: Book) -> tuple[int, str]:
        try:
            return (0, f"{int(b.id):08d}")
        except ValueError:
            return (1, b.id)

    return sorted(books, key=sort_key)


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormatSpec:
    name: str  # as the user typed it (after aliasing)
    kind: str  # "pattern" | "audio" | "all" | "ext"
    ext: str = ""  # for kind == "ext"


def parse_formats(value: str) -> tuple[list[FormatSpec], bool]:
    """
    Parse --format into specs. Returns (specs, first_available).

    "any" expands to epub,txt,html,kindle,audio and forces first-available
    behaviour. Unknown tokens that look like a file extension (mp3, pdf, zip ...)
    become extension filters applied to the files on the book's page.
    """
    tokens: list[str] = []
    for raw in value.split(","):
        t = raw.strip().lower().lstrip(".")
        if t:
            tokens.append(FORMAT_ALIASES.get(t, t))
    if not tokens:
        raise ValueError("--format needs at least one format")

    first = False
    expanded: list[str] = []
    for t in tokens:
        if t == "any":
            expanded.extend(ANY_ORDER)
            first = True
        else:
            expanded.append(t)
    if "all" in expanded:
        return [FormatSpec("all", "all")], False

    specs: list[FormatSpec] = []
    for t in dict.fromkeys(expanded):
        if t in PATTERN_FORMATS:
            specs.append(FormatSpec(t, "pattern"))
        elif t == "audio":
            specs.append(FormatSpec(t, "audio"))
        elif re.fullmatch(r"[a-z0-9]{2,5}", t):
            logger.info("--format %r is not a built-in format; matching files with that extension.", t)
            specs.append(FormatSpec(t, "ext", ext=t))
        else:
            raise ValueError(f"bad format {t!r}")
    return specs, first


def needs_sound(specs: list[FormatSpec]) -> bool:
    return any(
        s.kind in ("audio", "all") or (s.kind == "ext" and s.ext in AUDIO_EXTS)
        for s in specs
    )


def needs_text(specs: list[FormatSpec]) -> bool:
    return any(
        s.kind in ("pattern", "all") or (s.kind == "ext" and s.ext not in AUDIO_EXTS)
        for s in specs
    )


def spec_applies(spec: FormatSpec, book: Book) -> bool:
    """Audio formats live on Sound items, text formats on everything else."""
    is_sound = book.type.lower() == "sound"
    if spec.kind == "pattern":
        return not is_sound
    if spec.kind == "audio" or (spec.kind == "ext" and spec.ext in AUDIO_EXTS):
        return is_sound
    return True


# ---------------------------------------------------------------------------
# Book pages (used for audio and "all")
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteFile:
    url: str
    name: str


def extract_file_links(page: str, book_id: str) -> list[RemoteFile]:
    """Find the downloadable files linked from a book's /ebooks/{id} page."""
    page_url = f"{SITE}/ebooks/{book_id}"
    hosts = {urlparse(SITE).netloc.lower(), "www.gutenberg.org", "gutenberg.org"}
    files: list[RemoteFile] = []
    seen_urls: set[str] = set()
    used_names: set[str] = set()

    for href in re.findall(r"""href\s*=\s*["']([^"']+)["']""", page, flags=re.I):
        url = urljoin(page_url, html.unescape(href)).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.netloc.lower() not in hosts or parsed.path.endswith("/"):
            continue
        path = parsed.path
        name: str | None = None
        m = re.fullmatch(rf"/ebooks/{book_id}\.(.+)", path)
        if m:
            suffix = m.group(1)
            ext = EBOOK_LINK_EXT.get(suffix.split(".")[0])
            if ext:
                name = f"{book_id}.{suffix}{ext}"
        elif path.startswith((f"/files/{book_id}/", f"/cache/epub/{book_id}/")):
            name = unquote(path.rsplit("/", 1)[-1])
        if not name or url in seen_urls:
            continue
        seen_urls.add(url)
        stem, dot, ext = name.rpartition(".")
        n = 1
        candidate = name
        while candidate in used_names:  # same file name in different folders
            n += 1
            candidate = f"{stem}-{n}.{ext}" if dot else f"{name}-{n}"
        used_names.add(candidate)
        files.append(RemoteFile(url=url, name=candidate))
    return files


def discover_files(book_id: str, limiter: RateLimiter | None) -> list[RemoteFile]:
    r = get_with_retries(f"{SITE}/ebooks/{book_id}", limiter=limiter)
    return extract_file_links(r.text, book_id)


def spec_matches_file(spec: FormatSpec, name: str) -> bool:
    if spec.kind == "all":
        return True
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if spec.kind == "audio":
        return ext in AUDIO_EXTS
    return ext == spec.ext


def file_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def pick_audio_encoding(files: list[RemoteFile]) -> list[RemoteFile]:
    """Keep only one encoding of an audiobook: the most preferred one on offer."""
    present = {file_ext(f.name) for f in files}
    best = next((e for e in AUDIO_PREFERENCE if e in present), None)
    return [f for f in files if file_ext(f.name) == best] if best else []


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

def validate_download(dest_name: str, path: Path, content_type: str) -> str | None:
    """Return a problem description if the downloaded file is obviously wrong."""
    suffix = Path(dest_name).suffix.lower()
    if suffix in (".html", ".htm"):
        return None
    with open(path, "rb") as f:
        head = f.read(512)
    if suffix in (".epub", ".zip") and not head.startswith(b"PK"):
        return "response is not a valid zip/EPUB file"
    if "text/html" in content_type or head.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    ):
        return "server returned an HTML page instead of the file"
    return None


def _try_download(url: str, dest: Path, limiter: RateLimiter | None) -> tuple[str, str]:
    """
    Download one URL. Returns (status, message) with status one of
    "ok", "missing" (404/410), "failed", "cancelled".

    A transfer that breaks off or is cut short is retried.
    """
    tmp = dest.with_name(dest.name + ".part")
    last = "unknown error"
    for attempt in range(1, TRANSFER_ATTEMPTS + 1):
        try:
            with get_with_retries(url, attempts=3, stream=True, limiter=limiter) as r:
                content_type = (r.headers.get("Content-Type") or "").lower()
                expected = r.headers.get("Content-Length") or ""
                compressed = bool(r.headers.get("Content-Encoding"))
                written = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if STOP.is_set():
                            raise Cancelled()
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)

            if written == 0:
                tmp.unlink(missing_ok=True)
                return "failed", "empty response"
            if expected.isdigit() and not compressed and written < int(expected):
                raise Truncated(f"got {written} of {expected} bytes")
            problem = validate_download(dest.name, tmp, content_type)
            if problem:
                tmp.unlink(missing_ok=True)
                return "failed", problem
            tmp.replace(dest)
            return "ok", human_size(written)
        except Cancelled:
            tmp.unlink(missing_ok=True)
            return "cancelled", "interrupted"
        except requests.HTTPError as e:
            tmp.unlink(missing_ok=True)
            code = e.response.status_code if e.response is not None else 0
            return ("missing" if code in (404, 410) else "failed"), describe_error(e)
        except requests.RequestException as e:  # connection dropped, truncated, ...
            tmp.unlink(missing_ok=True)
            last = str(e)
        except OSError as e:  # disk full, permissions, ...
            tmp.unlink(missing_ok=True)
            return "failed", f"file error: {e}"
        if attempt < TRANSFER_ATTEMPTS:
            logger.warning("Transfer interrupted (%s); retrying ...", last)
            if STOP.wait(3.0 * attempt):
                return "cancelled", "interrupted"
    return "failed", last


@dataclass
class FileResult:
    format: str
    status: str  # ok | skipped | unavailable | failed | cancelled | na
    message: str = ""
    path: str = ""
    url: str = ""


@dataclass
class BookResult:
    book: Book
    files: list[FileResult] = field(default_factory=list)


@dataclass
class Plan:
    out_dir: Path
    specs: list[FormatSpec]
    first_available: bool
    force: bool
    limiter: RateLimiter


def fetch_to(label: str, urls: tuple[str, ...], dest: Path, plan: Plan) -> FileResult:
    """Try each candidate URL until one works."""
    rel = dest.relative_to(plan.out_dir).as_posix()
    if dest.exists() and not plan.force:
        return FileResult(label, "skipped", "already present", rel)
    dest.parent.mkdir(parents=True, exist_ok=True)

    missing = 0
    failure = ""
    for n, url in enumerate(urls):
        status, msg = _try_download(url, dest, plan.limiter)
        if status == "ok":
            note = f", fallback URL #{n}" if n else ""
            return FileResult(label, "ok", msg + note, rel, url)
        if status == "cancelled":
            return FileResult(label, "cancelled", msg, rel, url)
        logger.debug("    %s -> %s (%s)", url, status, msg)
        if status == "missing":
            missing += 1
        else:
            failure = f"{msg} ({url})"
    if failure:
        return FileResult(label, "failed", failure, rel, urls[-1])
    return FileResult(label, "unavailable", f"not offered ({missing} URL(s) returned 404)", rel)


def log_file_result(book: Book, res: FileResult) -> None:
    prefix = f"    [{book.id}] {res.format}:"
    name = res.path.rsplit("/", 1)[-1]
    if res.status == "ok":
        logger.info("%s downloaded %s (%s)", prefix, name, res.message)
    elif res.status == "skipped":
        logger.info("%s already present (%s)", prefix, name)
    elif res.status == "unavailable":
        logger.info("%s not available (%s)", prefix, res.message)
    elif res.status == "cancelled":
        logger.info("%s cancelled", prefix)
    elif res.status == "failed":
        logger.warning("%s FAILED: %s", prefix, res.message)


def process_book(book: Book, plan: Plan, index: int, total: int) -> BookResult:
    """Download every requested format of one book."""
    logger.info("[%d/%d] %s - %s", index, total, book.id, book.display_title())
    title = safe_filename(book.display_title())
    result = BookResult(book)
    page_files: list[RemoteFile] | None = None
    page_error: str | None = None

    for spec in plan.specs:
        if STOP.is_set():
            break
        if not spec_applies(spec, book):
            result.files.append(
                FileResult(spec.name, "na", f"not applicable to {book.type} items")
            )
            continue

        spec_results: list[FileResult] = []
        if spec.kind == "pattern":
            ext, patterns = PATTERN_FORMATS[spec.name]
            urls = tuple(p.format(site=SITE, id=book.id) for p in patterns)
            dest = plan.out_dir / f"{book.id} - {title}{ext}"
            spec_results.append(fetch_to(spec.name, urls, dest, plan))
        else:
            if page_files is None and page_error is None:
                try:
                    page_files = discover_files(book.id, plan.limiter)
                except requests.RequestException as e:
                    page_error = describe_error(e)
            if page_error is not None:
                spec_results.append(
                    FileResult(spec.name, "failed", f"could not read the book page: {page_error}")
                )
            else:
                matches = [f for f in (page_files or []) if spec_matches_file(spec, f.name)]
                if spec.kind == "audio":
                    matches = pick_audio_encoding(matches)
                if not matches:
                    spec_results.append(
                        FileResult(spec.name, "unavailable", "no matching files on the book page")
                    )
                for rf in matches:
                    dest = plan.out_dir / f"{book.id} - {title}" / safe_filename(rf.name)
                    spec_results.append(fetch_to(spec.name, (rf.url,), dest, plan))

        for res in spec_results:
            log_file_result(book, res)
        result.files.extend(spec_results)

        if plan.first_available and spec_results and all(
            r.status in ("ok", "skipped") for r in spec_results
        ):
            break

    if result.files and all(f.status == "na" for f in result.files):
        logger.warning(
            "    [%s] none of the requested formats apply to %s items", book.id, book.type
        )
    return result


def write_manifest(out_dir: Path, books: list[Book], results: dict[str, BookResult]) -> None:
    """Write a JSON manifest of the run."""
    entries = []
    for b in books:
        res = results.get(b.id)
        entries.append({
            "id": b.id,
            "title": b.display_title(),
            "type": b.type,
            "authors": list(b.authors),
            "languages": list(b.languages),
            "attempted": res is not None,
            "files": [
                {"format": f.format, "status": f.status, "message": f.message,
                 "path": f.path, "url": f.url}
                for f in (res.files if res else [])
            ],
        })
    payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "books": entries}
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("Manifest written to %s", path)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download books from Project Gutenberg by author, title, ID or category.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "authors", nargs="*", metavar="AUTHOR",
        help='One or more author names, e.g. "Dickens, Charles" "Jane Austen"',
    )
    p.add_argument(
        "--id", action="append", default=[], metavar="IDS",
        help="Gutenberg book ID(s): 5148, 5148,1342 or a range 100-120 (repeatable)",
    )
    p.add_argument(
        "--title", action="append", default=[], metavar="TEXT",
        help="Book title; every word must appear in the title (repeatable)",
    )
    p.add_argument(
        "--category", action="append", default=[], metavar="TEXT",
        help="Category: matched as text inside bookshelf names and subjects "
        '(repeatable), e.g. "Detective" or "Category: Poetry"',
    )
    p.add_argument(
        "--list-categories", nargs="?", const="", default=None, metavar="TEXT",
        help="Print the category names (optionally only those containing TEXT) and exit",
    )
    p.add_argument(
        "--format", default=None, metavar="LIST",
        help="Comma-separated: txt, epub, html, htmlzip, kindle, audio (one encoding per "
        "book, mp3 preferred), any, all, or a file extension such as mp3/ogg/m4b/spx/pdf "
        "(default: txt; audio for --type sound)",
    )
    p.add_argument(
        "--first-available", action="store_true",
        help="For each book, stop after the first listed format that exists",
    )
    p.add_argument(
        "--type", default=None, metavar="LIST",
        help="Catalog item types to search: text, sound, image, movingimage, dataset, "
        "collection, or any (default: text, plus sound when an audio format is requested)",
    )
    p.add_argument("--out", default=None, metavar="DIR", help="Output directory")
    p.add_argument(
        "--lang", default=None, metavar="CODES",
        help='Language code(s), comma-separated, e.g. "en" or "en,fr"',
    )
    p.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Only take the first N matches (lowest IDs first)",
    )
    p.add_argument(
        "--delay", type=float, default=ROBOT_DELAY, metavar="SEC",
        help="Minimum seconds between requests to gutenberg.org, shared by all "
        "workers (default: 2, per Gutenberg's robot policy)",
    )
    p.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Parallel transfers (default: 1). Requests still start --delay apart, "
        "so extra workers only overlap slow downloads.",
    )
    p.add_argument(
        "--source", choices=["auto", "catalog", "gutendex"], default="auto",
        help="Lookup source (default: auto = catalog, then Gutendex)",
    )
    p.add_argument(
        "--catalog", default=None, metavar="PATH",
        help="Local pg_catalog.csv to use instead of downloading one",
    )
    p.add_argument(
        "--include-contributors", action="store_true",
        help="Also match books where the author is only a [Contributor], "
        "[Illustrator], [Editor] or [Translator]",
    )
    p.add_argument(
        "--sole-author", action="store_true",
        help="Only books whose author list is exactly the matched author",
    )
    p.add_argument("--list-only", action="store_true", help="List matches without downloading")
    p.add_argument("--force", action="store_true", help="Re-download files that already exist")
    p.add_argument("--no-manifest", action="store_true", help="Do not write manifest.json")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="Only warnings and errors")
    return p


def configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def default_out_dir(authors: list[str]) -> Path:
    if len(authors) == 1:
        return Path(safe_filename(authors[0]).replace(" ", "_").replace(",", ""))
    return Path("gutenberg_books")


def warn_about_selection(sel: Selector, books: list[Book]) -> None:
    if sel._author_q and not sel.titles and not sel.categories and not sel.id_ranges:
        if any(len(q) == 1 for q in sel._author_q) and len(books) > 15:
            logger.warning(
                "A single-word author query matched %d books. If you meant one person, "
                'use a fuller name (e.g. "Jane Austen" instead of "Austen").',
                len(books),
            )
    if sel.include_contributors and sel._author_q:
        secondary = {
            n for b in books for n in b.authors if has_role(n) and sel.author_hit(n)
        }
        if secondary:
            sample = sorted(secondary)[:5]
            more = len(secondary) - len(sample)
            logger.warning(
                "Some matches list the author only in a secondary role "
                "(contributor, illustrator, editor, translator). Examples: %s%s",
                ", ".join(sample), f", ... (+{more} more)" if more > 0 else "",
            )
    if sel.id_ranges:
        found = {b.id for b in books}
        missing = [str(lo) for lo, hi in sel.id_ranges if lo == hi and str(lo) not in found]
        if missing:
            logger.warning(
                "Not found (wrong ID, wrong type, or filtered out): %s", ", ".join(missing)
            )


def main() -> None:
    STOP.clear()
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose, args.quiet)

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.delay < 0:
        parser.error("--delay must be non-negative")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.catalog and args.source == "gutendex":
        parser.error("--catalog cannot be combined with --source gutendex")
    if args.delay < ROBOT_DELAY:
        logger.warning(
            "--delay %.1fs is below the 2s that Project Gutenberg's robot policy asks for.",
            args.delay,
        )

    authors = [a for a in args.authors if a.strip()]
    for q in authors:
        if not query_words(q):
            parser.error(f"author {q!r} has no searchable words")
    try:
        id_ranges = parse_ids(args.id)
        explicit_types = parse_types(args.type) if args.type else None
        fmt_value = args.format
        if fmt_value is None:
            only_sound = bool(args.type) and explicit_types is not None and explicit_types <= {"sound"}
            fmt_value = "audio" if only_sound else "txt"
        specs, first = parse_formats(fmt_value)
    except ValueError as e:
        parser.error(str(e))
    first = first or args.first_available

    if args.type:
        types = explicit_types
    else:
        types = ({"text"} if needs_text(specs) else set()) | (
            {"sound"} if needs_sound(specs) else set()
        )

    catalog_path = Path(args.catalog) if args.catalog else None

    if args.list_categories is not None:
        try:
            rows = list_categories(catalog_path, types, args.list_categories)
        except requests.RequestException as e:
            sys.exit(f"Could not get the Gutenberg catalog: {e}")
        if not rows:
            sys.exit("No categories found.")
        for name, count in rows:
            print(f"{count:7d}  {name}")
        logger.info(
            "%d categories. --category also matches subject headings, not just these names.",
            len(rows),
        )
        return

    titles = [t for t in args.title if t.strip()]
    categories = [c for c in args.category if c.strip()]
    if not (authors or titles or categories or id_ranges):
        parser.error(
            "choose what to download: give an author, or use --id, --title or --category "
            "(or --list-categories to browse categories)"
        )
    if args.sole_author and not authors:
        logger.warning("--sole-author only has an effect together with an author name.")

    sel = Selector(
        authors=authors, titles=titles, categories=categories, id_ranges=id_ranges,
        langs={x.strip().lower() for x in args.lang.split(",") if x.strip()} if args.lang else None,
        types=types, include_contributors=args.include_contributors, sole_author=args.sole_author,
    )

    books = resolve_books(args, sel)
    if args.limit:
        books = books[: args.limit]
    if not books:
        sys.exit(
            "No books found. Try a different spelling, fewer filters, --include-contributors, "
            "or --type any."
        )

    if authors:
        matched = sorted({n for b in books for n in b.authors if sel.author_hit(n)
                          and (sel.include_contributors or not has_role(n))})
        logger.info("Found %d item(s). Matched author(s): %s", len(books), ", ".join(matched) or "(none)")
    else:
        logger.info("Found %d item(s).", len(books))
    warn_about_selection(sel, books)

    if args.list_only:
        for b in books:
            langs = f" [{','.join(b.languages)}]" if b.languages else ""
            kind = f" ({b.type})" if b.type.lower() != "text" else ""
            print(f"  [{b.id}] {b.display_title()}{langs}{kind}")
        return

    out_dir = Path(args.out).expanduser() if args.out else default_out_dir(authors)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create output directory {out_dir}: {e}")

    total = len(books)
    logger.info(
        "Note: gutenberg.org says it is for human users and may block IPs that use automated "
        "tools outside its mirror/harvest options (see README, 'Robot policy'). "
        "Keep selections small; requests are paced %.0fs apart.",
        args.delay,
    )
    minutes = total * len(specs) * args.delay / 60
    if minutes >= 10:
        logger.info(
            "Note: with a %.0fs delay this takes at least ~%.0f minutes if nothing is skipped.",
            args.delay, minutes,
        )

    plan = Plan(out_dir=out_dir, specs=specs, first_available=first, force=args.force,
                limiter=RateLimiter(args.delay))
    results: dict[str, BookResult] = {}
    counts: Counter[str] = Counter()

    def task(i: int, book: Book) -> BookResult:
        try:
            return process_book(book, plan, i, total)
        except Cancelled:
            return BookResult(book, [FileResult("-", "cancelled", "interrupted")])
        except Exception as e:  # keep one bad book from stopping the run
            logger.warning("    [%s] unexpected error: %s", book.id, e)
            return BookResult(book, [FileResult("-", "failed", f"unexpected error: {e}")])

    def tally(res: BookResult) -> None:
        results[res.book.id] = res
        for f in res.files:
            if f.status != "na":
                counts[f.status] += 1

    interrupted = False
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(task, i, b): b for i, b in enumerate(books, 1)}
    try:
        for fut in as_completed(futures):
            tally(fut.result())
    except KeyboardInterrupt:
        interrupted = True
        STOP.set()
        logger.warning("\nInterrupted - stopping after in-flight transfers abort ...")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if interrupted:
        for fut, book in futures.items():
            if book.id in results or fut.cancelled() or not fut.done():
                continue
            try:
                tally(fut.result())
            except Exception:
                pass

    if not args.no_manifest:
        try:
            write_manifest(out_dir, books, results)
        except OSError as e:
            logger.warning("Could not write manifest: %s", e)

    not_done = total - len({k for k, v in results.items() if all(f.status != "cancelled" for f in v.files)})
    logger.info(
        "%s. Files downloaded: %d, already present: %d, not available: %d, failed: %d%s",
        "Interrupted" if interrupted else "Finished",
        counts["ok"], counts["skipped"], counts["unavailable"], counts["failed"],
        f", books not completed: {not_done}" if interrupted else "",
    )
    logger.info("Saved under: %s", out_dir.resolve())
    if interrupted:
        sys.exit(130)
    if counts["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP.set()
        logging.getLogger("gutenberg").warning("\nInterrupted by user (Ctrl+C).")
        sys.exit(130)
