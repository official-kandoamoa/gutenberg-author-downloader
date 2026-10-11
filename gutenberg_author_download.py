#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
Download all books by a given author from Project Gutenberg.

Book lookup:
  1. Gutendex API (https://gutendex.com), retried with exponential backoff.
  2. If Gutendex is unavailable (or --source catalog / --catalog is used),
     falls back to Project Gutenberg's official catalog (pg_catalog.csv).
     Supply a local catalog with --catalog PATH to skip downloading it.

By default only people credited as a primary author are matched, so books
where the name appears only as [Contributor], [Illustrator], [Editor] or
[Translator] are left out (use --include-contributors to keep them).

Usage examples:
    uv run gutenberg_author_download.py "Dickens, Charles"
    uv run gutenberg_author_download.py "Jane Austen" --format epub --lang en
    uv run gutenberg_author_download.py "Twain" --list-only
    uv run gutenberg_author_download.py "Doyle, Arthur Conan, 1859-1930" --lang en --sole-author
    uv run gutenberg_author_download.py "Jane Austen" --catalog ~/pg_catalog.csv
    uv run gutenberg_author_download.py "Austen" --format epub --workers 3
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = "https://gutendex.com/books/"
CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
CATALOG_CACHE = Path.home() / ".cache" / "gutenberg" / "pg_catalog.csv"
CATALOG_MAX_AGE = 7 * 24 * 3600  # seconds
CATALOG_STALE_WARN = 30 * 24 * 3600  # warn if a local/cached file is older
ROBOT_DELAY = 2.0  # seconds, per Project Gutenberg's robot access policy

RETRY_STATUS = {429, 500, 502, 503, 504}

FORMAT_MIMES: dict[str, list[str]] = {
    "txt": [
        "text/plain; charset=utf-8",
        "text/plain; charset=us-ascii",
        "text/plain",
    ],
    "epub": ["application/epub+zip"],
    "html": ["text/html; charset=utf-8", "text/html"],
    "kindle": ["application/x-mobipocket-ebook"],
}

EXTENSIONS: dict[str, str] = {
    "txt": ".txt",
    "epub": ".epub",
    "html": ".html",
    "kindle": ".mobi",
}

# Candidate download URLs, tried in order. A failed candidate (404, HTML error
# page, ...) moves on to the next one. {id} is the Gutenberg book number.
CATALOG_URLS: dict[str, tuple[str, ...]] = {
    "txt": (
        "https://www.gutenberg.org/ebooks/{id}.txt.utf-8",
        "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
        "https://www.gutenberg.org/files/{id}/{id}-0.txt",
        "https://www.gutenberg.org/files/{id}/{id}.txt",
    ),
    "epub": (
        "https://www.gutenberg.org/ebooks/{id}.epub3.images",
        "https://www.gutenberg.org/ebooks/{id}.epub.images",
        "https://www.gutenberg.org/ebooks/{id}.epub.noimages",
        "https://www.gutenberg.org/cache/epub/{id}/pg{id}.epub",
    ),
    "html": (
        "https://www.gutenberg.org/cache/epub/{id}/pg{id}-images.html",
        "https://www.gutenberg.org/files/{id}/{id}-h/{id}-h.htm",
    ),
    "kindle": (
        "https://www.gutenberg.org/ebooks/{id}.kf8.images",
        "https://www.gutenberg.org/ebooks/{id}.kindle.images",
        "https://www.gutenberg.org/ebooks/{id}.kindle.noimages",
    ),
}

USER_AGENT = (
    "gutenberg-author-downloader/3.0 "
    "(personal use; +https://www.gutenberg.org/policy/robot_access.html)"
)

logger = logging.getLogger("gutenberg")

# Set on Ctrl+C so worker threads stop promptly.
STOP = threading.Event()


class Cancelled(Exception):
    """Raised inside worker threads when the user interrupts the run."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Book:
    id: str
    title: str
    authors: tuple[str, ...]
    urls: tuple[str, ...]  # candidate download URLs, best first
    languages: tuple[str, ...] = ()

    @property
    def url(self) -> str | None:
        return self.urls[0] if self.urls else None

    @property
    def alt_urls(self) -> tuple[str, ...]:
        return self.urls[1:]

    def display_title(self) -> str:
        return self.title.replace("\n", " ").replace("\r", " ").strip()


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

_ROLE_RE = re.compile(r"\[[^\]]*\]\s*$")


def normalize(name: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def has_role(name: str) -> bool:
    """True for catalog names tagged with a role, e.g. '... [Illustrator]'."""
    return bool(_ROLE_RE.search(name))


def _author_core(name: str) -> str:
    """Strip roles, parentheticals, and dates from an author string."""
    base = re.sub(r"\(.*?\)", " ", name)
    base = re.sub(r"\[.*?\]", " ", base)
    base = re.sub(r"\b\d{4}\b", " ", base)
    return normalize(base)


def query_words(query: str) -> list[str]:
    """Significant words of a query: no years, no single letters."""
    cleaned = re.sub(r"\b\d{4}\b", " ", query)
    return [w for w in normalize(cleaned).split() if len(w) > 1]


def name_matches(query: str, names: Iterable[str]) -> bool:
    """
    True if every significant word of the query appears in at least one of the
    candidate author names.

    Handles forms such as "Dickens, Charles" and "Austen, Jane, 1775-1817".
    Dates and role markers are ignored on both sides.
    """
    q_words = set(query_words(query))
    if not q_words:
        return False
    return any(q_words <= set(_author_core(n).split()) for n in names)


def book_matches(
    query: str,
    names: list[str],
    *,
    include_contributors: bool,
    sole_author: bool,
) -> bool:
    """
    Decide whether a book belongs to the queried author.

    - default: the person must appear without a role tag (a primary author).
    - include_contributors: [Contributor]/[Illustrator]/... entries count too.
    - sole_author: the book must list exactly one author entry (so co-authored
      books and books with illustrators/editors listed are excluded).
    """
    if sole_author and len(names) != 1:
        return False
    candidates = names if include_contributors else [n for n in names if not has_role(n)]
    return name_matches(query, candidates)


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
# Gutendex
# ---------------------------------------------------------------------------

def candidate_urls(book_id: str, fmt: str, first: str | None = None) -> tuple[str, ...]:
    urls = [first] if first else []
    urls += [pattern.format(id=book_id) for pattern in CATALOG_URLS[fmt]]
    return tuple(dict.fromkeys(urls))


def _select_format_url(formats: dict[str, str], fmt: str) -> str | None:
    """Pick the best Gutendex URL for the requested format."""
    for mime in FORMAT_MIMES[fmt]:
        if mime in formats:
            return formats[mime]
    base = FORMAT_MIMES[fmt][-1]
    for key, value in formats.items():
        if key.startswith(base):
            return value
    return None


def fetch_from_gutendex(
    author: str,
    languages: str | None,
    fmt: str,
    *,
    include_contributors: bool = False,
    sole_author: bool = False,
) -> list[Book]:
    """Query Gutendex and return matching books (deduplicated by ID)."""
    # Gutendex author names carry no dates, so years must not be in the search.
    params: dict[str, Any] = {"search": " ".join(query_words(author))}
    if languages:
        params["languages"] = languages

    books_by_id: dict[str, Book] = {}
    url: str | None = API_URL
    page = 0

    while url:
        page += 1
        logger.debug("Gutendex page %d", page)
        data = get_with_retries(url, params=params).json()
        for b in data.get("results", []):
            names = [a["name"] for a in b.get("authors", [])]
            if not book_matches(
                author,
                names,
                include_contributors=include_contributors,
                sole_author=sole_author,
            ):
                continue
            book_id = str(b["id"])
            link = _select_format_url(b.get("formats") or {}, fmt)
            books_by_id[book_id] = Book(
                id=book_id,
                title=b.get("title") or f"Untitled {book_id}",
                authors=tuple(names),
                urls=candidate_urls(book_id, fmt, link),
                languages=tuple(b.get("languages") or []),
            )
        url = data.get("next")
        params = None  # subsequent pages carry the query in the "next" URL

    return list(books_by_id.values())


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def load_catalog(catalog_path: Path | None = None) -> Path:
    """
    Return the path to the catalog CSV.

    A user-supplied path is used as-is (never downloaded or overwritten).
    Otherwise the cache file is used, and refreshed when missing or old.
    """
    if catalog_path is not None:
        path = catalog_path.expanduser().resolve()
        if not path.is_file():
            sys.exit(f"Catalog file not found: {path}")
        age = time.time() - path.stat().st_mtime
        if age > CATALOG_STALE_WARN:
            logger.warning(
                "Local catalog is %d days old (%s). Consider re-downloading from %s",
                int(age / 86400),
                path,
                CATALOG_URL,
            )
        logger.info("Using local catalog: %s", path)
        return path

    if (
        CATALOG_CACHE.exists()
        and time.time() - CATALOG_CACHE.stat().st_mtime < CATALOG_MAX_AGE
    ):
        age = time.time() - CATALOG_CACHE.stat().st_mtime
        logger.info("Using cached catalog: %s (%.0f hours old)", CATALOG_CACHE, age / 3600)
        return CATALOG_CACHE

    logger.info("Downloading Project Gutenberg catalog (cached for one week) ...")
    CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    r = get_with_retries(CATALOG_URL)
    tmp = CATALOG_CACHE.with_suffix(".csv.part")
    tmp.write_bytes(r.content)
    tmp.replace(CATALOG_CACHE)
    return CATALOG_CACHE


def fetch_from_catalog(
    author: str,
    languages: str | None,
    fmt: str,
    catalog_path: Path | None = None,
    *,
    include_contributors: bool = False,
    sole_author: bool = False,
) -> list[Book]:
    """Scan the official catalog CSV for matching books."""
    wanted_langs = (
        {lang.strip().lower() for lang in languages.split(",") if lang.strip()}
        if languages
        else None
    )
    books_by_id: dict[str, Book] = {}
    path = load_catalog(catalog_path)

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

        for row in reader:
            if row.get("Type") != "Text":
                continue
            names = [
                a.strip() for a in (row.get("Authors") or "").split(";") if a.strip()
            ]
            if not book_matches(
                author,
                names,
                include_contributors=include_contributors,
                sole_author=sole_author,
            ):
                continue
            row_langs = {
                lang.strip().lower()
                for lang in (row.get("Language") or "").split(";")
                if lang.strip()
            }
            if wanted_langs and not (row_langs & wanted_langs):
                continue
            book_id = row["Text#"]
            title = (row.get("Title") or f"Untitled {book_id}").replace("\n", " ").replace("\r", " ")
            books_by_id[book_id] = Book(
                id=book_id,
                title=title,
                authors=tuple(names),
                urls=candidate_urls(book_id, fmt),
                languages=tuple(sorted(row_langs)),
            )

    return list(books_by_id.values())


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def safe_filename(text: str, max_len: int = 120) -> str:
    """Produce a filesystem-safe filename component."""
    text = re.sub(r"[\s ]+", " ", text)
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text)
    text = text.strip(" .")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" .")
    return text or "untitled"


def _validate(suffix: str, path: Path, content_type: str) -> str | None:
    """Return a problem description if the downloaded file is obviously wrong."""
    with open(path, "rb") as f:
        head = f.read(256)
    looks_html = "text/html" in content_type or head.lstrip().lower().startswith(
        (b"<!doctype html", b"<html")
    )
    if suffix == ".epub" and not head.startswith(b"PK"):
        return "response is not a valid EPUB (zip) file"
    if suffix == ".mobi" and looks_html:
        return "server returned HTML instead of the ebook file"
    return None


def _try_download(url: str, dest: Path, limiter: RateLimiter | None) -> tuple[str, str]:
    """Download one candidate URL. Returns (status, message)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with get_with_retries(url, attempts=3, stream=True, limiter=limiter) as r:
            content_type = (r.headers.get("Content-Type") or "").lower()
            expected = r.headers.get("Content-Length")
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

        problem = _validate(dest.suffix, tmp, content_type)
        if problem:
            tmp.unlink(missing_ok=True)
            return "failed", problem

        note = ""
        if expected is not None:
            try:
                if written != int(expected):
                    # Gutenberg's Content-Length is often wrong; keep the file.
                    note = f", size note: got {written} vs header {expected}"
            except ValueError:
                pass

        tmp.replace(dest)
        return "ok", f"{dest.stat().st_size / 1024:.0f} KiB{note}"
    except Cancelled:
        tmp.unlink(missing_ok=True)
        return "cancelled", "interrupted"
    except requests.HTTPError as e:
        tmp.unlink(missing_ok=True)
        code = e.response.status_code if e.response is not None else "error"
        return "failed", f"HTTP {code}"
    except (requests.RequestException, OSError) as e:
        tmp.unlink(missing_ok=True)
        return "failed", str(e)


def download_one(
    book: Book,
    dest: Path,
    *,
    force: bool = False,
    limiter: RateLimiter | None = None,
) -> tuple[str, str]:
    """
    Download a book, trying each candidate URL in turn.

    Returns (status, message); status is one of
    "ok", "skipped", "no_url", "failed", "cancelled".
    """
    if dest.exists() and not force:
        return "skipped", "already present"
    if not book.urls:
        return "no_url", "no suitable format available"

    last = "no attempt made"
    for n, url in enumerate(book.urls):
        status, msg = _try_download(url, dest, limiter)
        if status == "ok":
            return "ok", msg + (f", fallback URL #{n}" if n else "")
        if status == "cancelled":
            return status, msg
        logger.debug("    %s -> %s", url, msg)
        last = f"{msg} ({len(book.urls)} URL(s) tried; last: {url})"
    return "failed", last


def write_manifest(out_dir: Path, books: list[Book], results: dict[str, str]) -> None:
    """Write a JSON manifest of the run."""
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "books": [
            {
                "id": b.id,
                "title": b.display_title(),
                "authors": list(b.authors),
                "languages": list(b.languages),
                "url": b.url,
                "alt_urls": list(b.alt_urls),
                "status": results.get(b.id, "not_attempted"),
                "filename": f"{b.id} - {safe_filename(b.display_title())}",
            }
            for b in books
        ],
    }
    path = out_dir / "manifest.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Manifest written to %s", path)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def warn_about_query(author: str, books: list[Book], include_contributors: bool) -> None:
    """Emit helpful warnings about ambiguous or broad matches."""
    if len(query_words(author)) == 1 and len(books) > 15:
        logger.warning(
            "Query %r is a single word and matched %d books. "
            "If you meant a specific person, try a fuller name "
            '(e.g. "Jane Austen" instead of "Austen").',
            author,
            len(books),
        )

    if include_contributors:
        secondary = {
            n
            for b in books
            for n in b.authors
            if has_role(n) and name_matches(author, [n])
        }
        if secondary:
            sample = sorted(secondary)[:5]
            more = len(secondary) - len(sample)
            logger.warning(
                "Some matches list the query only in a secondary role "
                "(contributor, illustrator, editor, translator). Examples: %s%s",
                ", ".join(sample),
                f", ... (+{more} more)" if more > 0 else "",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download all Project Gutenberg books by a given author.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("author", help='Author name, e.g. "Dickens, Charles" or "Jane Austen"')
    p.add_argument(
        "--format", choices=list(FORMAT_MIMES), default="txt",
        help="Preferred file format (default: txt)",
    )
    p.add_argument(
        "--out", default=None, metavar="DIR",
        help="Output directory (default: derived from author name)",
    )
    p.add_argument(
        "--lang", default=None, metavar="CODES",
        help='Language code(s), comma-separated, e.g. "en" or "en,fr"',
    )
    p.add_argument(
        "--delay", type=float, default=ROBOT_DELAY, metavar="SEC",
        help="Minimum seconds between requests to gutenberg.org, shared by all "
        "workers (default: 2, per Gutenberg's robot policy)",
    )
    p.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help="Parallel transfers (default: 1). Requests still start --delay "
        "apart, so extra workers only overlap slow downloads.",
    )
    p.add_argument(
        "--source", choices=["auto", "gutendex", "catalog"], default="auto",
        help="Book lookup source (default: auto = Gutendex, then catalog)",
    )
    p.add_argument(
        "--catalog", default=None, metavar="PATH",
        help="Local pg_catalog.csv to read instead of downloading one "
        "(implies --source catalog unless --source gutendex is given)",
    )
    p.add_argument(
        "--include-contributors", action="store_true",
        help="Also match books where the author is only a [Contributor], "
        "[Illustrator], [Editor] or [Translator]",
    )
    p.add_argument(
        "--sole-author", action="store_true",
        help="Only books whose author list is exactly this one person "
        "(excludes co-authored books and ones with illustrators/editors)",
    )
    p.add_argument("--list-only", action="store_true", help="List matching books without downloading")
    p.add_argument("--force", action="store_true", help="Re-download even if the target file exists")
    p.add_argument("--no-manifest", action="store_true", help="Do not write manifest.json")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="Suppress progress messages")
    return p


def configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def resolve_books(args: argparse.Namespace) -> list[Book]:
    books: list[Book] | None = None
    catalog_path = Path(args.catalog) if args.catalog else None
    opts = {
        "include_contributors": args.include_contributors,
        "sole_author": args.sole_author,
    }

    if catalog_path is not None and args.source == "auto":
        args.source = "catalog"

    if args.source in ("auto", "gutendex"):
        logger.info("Searching Gutendex for author: %r ...", args.author)
        try:
            books = fetch_from_gutendex(args.author, args.lang, args.format, **opts)
        except requests.RequestException as e:
            if args.source == "gutendex":
                sys.exit(f"Gutendex unavailable: {e}")
            logger.warning("Gutendex unavailable (%s). Falling back to the official catalog.", e)

    if books is None:
        try:
            books = fetch_from_catalog(
                args.author, args.lang, args.format, catalog_path=catalog_path, **opts
            )
        except requests.RequestException as e:
            sys.exit(f"Could not download the Gutenberg catalog either: {e}")

    def sort_key(b: Book) -> tuple[int, str]:
        try:
            return (0, f"{int(b.id):08d}")
        except ValueError:
            return (1, b.id)

    return sorted(books, key=sort_key)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.verbose, args.quiet)

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.delay < 0:
        parser.error("--delay must be non-negative")
    if args.delay < ROBOT_DELAY:
        logger.warning(
            "--delay %.1fs is below the 2s that Project Gutenberg's robot policy asks for.",
            args.delay,
        )

    out_dir = Path(
        args.out or safe_filename(args.author).replace(" ", "_").replace(",", "")
    )

    books = resolve_books(args)
    if not books:
        sys.exit(
            "No books found. Try a different spelling, the surname only, "
            "--include-contributors, or --source catalog."
        )

    matched_authors = sorted(
        {
            n
            for b in books
            for n in b.authors
            if name_matches(args.author, [n])
            and (args.include_contributors or not has_role(n))
        }
    )
    logger.info(
        "Found %d book(s). Matched author(s): %s",
        len(books),
        ", ".join(matched_authors) or "(none)",
    )
    warn_about_query(args.author, books, args.include_contributors)

    if args.list_only:
        for b in books:
            langs = f" [{','.join(b.languages)}]" if b.languages else ""
            print(f"  [{b.id}] {b.display_title()}{langs}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    ext = EXTENSIONS[args.format]
    limiter = RateLimiter(args.delay)
    results: dict[str, str] = {}
    counts: Counter[str] = Counter()
    total = len(books)

    def task(i: int, book: Book) -> tuple[Book, str, str]:
        dest = out_dir / f"{book.id} - {safe_filename(book.display_title())}{ext}"
        logger.info("[%d/%d] %s", i, total, book.display_title())
        try:
            status, msg = download_one(book, dest, force=args.force, limiter=limiter)
        except Cancelled:
            status, msg = "cancelled", "interrupted"
        except Exception as e:  # keep one bad book from stopping the run
            status, msg = "failed", f"unexpected error: {e}"
        return book, status, msg

    def tally(book: Book, status: str, msg: str) -> None:
        results[book.id] = status
        counts[status] += 1
        title = book.display_title()
        if status == "ok":
            logger.info("    downloaded: %s (%s)", title, msg)
        elif status == "skipped":
            logger.info("    skipped: %s (%s)", title, msg)
        elif status == "cancelled":
            logger.info("    cancelled: %s", title)
        else:
            logger.warning("    %s: %s - %s", status, title, msg)

    interrupted = False
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {pool.submit(task, i, b): b for i, b in enumerate(books, 1)}
    try:
        for fut in as_completed(futures):
            tally(*fut.result())
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
                tally(*fut.result())
            except Exception:
                pass

    if not args.no_manifest:
        try:
            write_manifest(out_dir, books, results)
        except OSError as e:
            logger.warning("Could not write manifest: %s", e)

    not_started = total - sum(counts.values())
    logger.info(
        "%s. Downloaded: %d, skipped: %d, failed/unavailable: %d%s",
        "Interrupted" if interrupted else "Finished",
        counts["ok"],
        counts["skipped"],
        counts["failed"] + counts["no_url"],
        f", not completed: {not_started + counts['cancelled']}" if interrupted else "",
    )
    logger.info("Saved under: %s", out_dir.resolve())
    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        STOP.set()
        logging.getLogger("gutenberg").warning("\nInterrupted by user (Ctrl+C).")
        sys.exit(130)
