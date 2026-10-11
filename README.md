## ⚖️ Legal Disclaimer

> [!CAUTION]
> **This software is provided for educational and research purposes only.** The author(s) assume no liability or responsibility for any unauthorized, illegal, or unethical use of this project.

### 1. No Liability
Under no circumstances shall the project owner, authors, or contributors be held liable for any direct, indirect, incidental, special, exemplary, or consequential damages. This includes, but is not limited to:
* **Financial loss** or loss of profits
* **Data corruption**, loss, or breach
* **System downtime**, service interruptions, or hardware damage
* **Legal consequences** resulting from your deployment or usage

### 2. "As-Is" Basis
This project is provided **"as is" and "as available"**, without warranty of any kind, express or implied. This includes warranties of merchantability, fitness for a particular purpose, or non-infringement. You assume all operational and technical risks by executing or modifying this code.

### 3. "Gray Area" Use Cases
If this software is deployed in ambiguous, high-risk, or production environments, the user bears **100% of the responsibility** to ensure compliance with local laws, corporate policies, and third-party terms of service. 


> [!WARNING]
> **AI development disclosure:** This project was developed with assistance from the free versions of ChatGPT, Grok, and Claude (LLMs), with the user providing ideas and testing while the AIs and user collaboratively suggested, generated, reviewed, and refined code and solutions.


> [!WARNING]
> **This project is in early development.** Expect bugs, missing features, and frequent breaking changes.

---

# Gutenberg Author Downloader

Download Project Gutenberg books by author name.

```bash
uv run gutenberg_author_download.py "Jane Austen"
uv run gutenberg_author_download.py "Doyle, Arthur Conan, 1859-1930" --catalog ./pg_catalog.csv --lang en --format epub
```

**Requirements:** Python 3.10+, `requests`  
**Run with uv:** `uv run gutenberg_author_download.py ...` (installs `requests` automatically)  
**Run with pip:** `pip install requests && python gutenberg_author_download.py ...`

---

## Table of contents

1. [All arguments and options](#all-arguments-and-options)
2. [Author matching](#author-matching)
3. [Options (full detail)](#options-full-detail)
4. [How lookup works](#how-lookup-works)
5. [Download formats and URL fallbacks](#download-formats-and-url-fallbacks)
6. [Output files](#output-files)
7. [Behaviour notes](#behaviour-notes)
8. [Examples](#examples)
9. [Troubleshooting](#troubleshooting)

---

## All arguments and options

```text
gutenberg_author_download.py
    author
    [--format {txt,epub,html,kindle}]
    [--out DIR]
    [--lang CODES]
    [--delay SEC]
    [--workers N]
    [--source {auto,gutendex,catalog}]
    [--catalog PATH]
    [--include-contributors]
    [--sole-author]
    [--list-only]
    [--force]
    [--no-manifest]
    [-v | --verbose]
    [-q | --quiet]
    [-h | --help]
```

| Name | Type | Required | Default | Short summary |
|------|------|----------|---------|---------------|
| `author` | string | **yes** | - | Author name to search for |
| `--format` | choice | no | `txt` | File format: `txt`, `epub`, `html`, `kindle` |
| `--out` | path | no | from author name | Output directory |
| `--lang` | string | no | all languages | Language filter, e.g. `en` or `en,fr` |
| `--delay` | float | no | `2.0` | Minimum seconds between requests to gutenberg.org (shared by all workers) |
| `--workers` | int | no | `1` | Parallel transfers; request starts are still `--delay` apart |
| `--source` | choice | no | `auto` | Where to look up books: `auto`, `gutendex`, `catalog` |
| `--catalog` | path | no | - | Path to a local `pg_catalog.csv` |
| `--include-contributors` | flag | no | off | Also match `[Contributor]`, `[Illustrator]`, `[Editor]`, `[Translator]` entries |
| `--sole-author` | flag | no | off | Only books whose author list is exactly this one person |
| `--list-only` | flag | no | off | List matches; do not download |
| `--force` | flag | no | off | Re-download even if the file exists |
| `--no-manifest` | flag | no | off | Do not write `manifest.json` |
| `-v` / `--verbose` | flag | no | off | Debug logging |
| `-q` / `--quiet` | flag | no | off | Only warnings and errors |
| `-h` / `--help` | flag | no | - | Show help and exit |

---

## Author matching

**`author`** is required. Examples:

| Example | Notes |
|---------|-------|
| `"Jane Austen"` | Recommended form (given name + surname) |
| `"Doyle, Arthur Conan"` | Catalog-style order also works |
| `"Doyle, Arthur Conan, 1859-1930"` | Years in the query are ignored for matching |
| `"Conan Doyle"` | Partial names work if every word appears in the catalog name |
| `"Austen"` | Broad: matches every author with that word (a warning is shown) |

**Rules**

- Case-insensitive; punctuation, dates and single letters are ignored.
- Every word of the query must appear in one author name.
- Only catalog rows with `Type` = `Text` are included (not sound, images, datasets).
- **By default only primary authors match.** The catalog tags secondary credits, for example `Doyle, Arthur Conan, 1859-1930 [Contributor]`. Those entries are ignored, so anthologies that merely include one story by the author are left out. Use `--include-contributors` to bring them back.
- **`--sole-author`** is stricter: the book's author list must be exactly one entry, so co-authored books and books with a listed illustrator or editor are dropped too. It is equivalent to matching the catalog `Authors` field exactly, like the regex `Text(.*),en,"Doyle, Arthur Conan, 1859-1930"`.

**How the modes compare** (catalog snapshot with 79,533 rows, `"Doyle, Arthur Conan, 1859-1930" --lang en`):

| Mode | Books |
|------|-------|
| `--sole-author` | 80 |
| default (primary authors) | 84 |
| `--include-contributors` | 90 |

The 4 extra books in the default mode are co-credited works, for example *Round the Fire Stories* with its illustrator. Counts on gutenberg.org can differ from the catalog because the same title often exists as several editions with different IDs (for example IDs 108 and 221, *The Return of Sherlock Holmes*), and each ID counts as a separate book here.

---

## Options (full detail)

### `--format {txt,epub,html,kindle}`

**Default:** `txt`

| Value | Extension saved | Notes |
|-------|-----------------|-------|
| `txt` | `.txt` | Plain text (smallest) |
| `epub` | `.epub` | EPUB3 with images preferred |
| `html` | `.html` | HTML with images |
| `kindle` | `.mobi` | Kindle / KF8 |

See [Download formats and URL fallbacks](#download-formats-and-url-fallbacks) for the exact URLs tried.

### `--out DIR`

**Default:** author name with spaces turned into `_` and commas removed.

| Author argument | Default folder |
|-----------------|----------------|
| `"Doyle, Arthur Conan"` | `Doyle_Arthur_Conan/` |
| `"Doyle, Arthur Conan, 1859-1930"` | `Doyle_Arthur_Conan_1859-1930/` |

Use the same `--out` to resume a previous run (existing files are skipped).

### `--lang CODES`

**Default:** all languages. Comma-separated ISO codes; a book is kept if any of its languages is in the set (`--lang en,fr`). Without it, translations are included.

### `--delay SEC`

**Default:** `2.0`, must be `>= 0`.

The minimum time between the starts of any two requests to gutenberg.org, enforced across all workers, including retries and fallback URLs. A value below 2 prints a warning because Project Gutenberg's [robot policy](https://www.gutenberg.org/policy/robot_access.html) asks for 2 seconds. Runs where every file already exists make no requests, so they are not slowed down.

### `--workers N`

**Default:** `1`, must be `>= 1`.

Number of parallel transfers. Because requests still start `--delay` apart, extra workers only help by overlapping slow downloads; they do not raise the request rate. `1` is fine for most uses.

### `--source {auto,gutendex,catalog}`

**Default:** `auto`

| Value | Behaviour |
|-------|-----------|
| `auto` | Gutendex first; on failure fall back to the catalog. With `--catalog`, switches to catalog mode. |
| `gutendex` | Gutendex only; exits with an error if unavailable. |
| `catalog` | Catalog only (local `--catalog` file, or a cached download). |

### `--catalog PATH`

Path to a local `pg_catalog.csv` (official feed: `https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv`). The file is never downloaded over or modified, and a warning appears if it is older than about 30 days. Required columns: `Text#`, `Type`, `Title`, `Authors` (`Language` is needed for `--lang`).

Without `--catalog`, a copy is cached at `~/.cache/gutenberg/pg_catalog.csv` and refreshed after 7 days.

### `--include-contributors`, `--sole-author`

See [Author matching](#author-matching). Gutendex lists translators separately from authors, so `--include-contributors` makes no difference in Gutendex mode; `--sole-author` works in both modes.

### `--list-only`

Prints matching books to stdout and exits. No files or folders are created.

```text
  [105] Persuasion [en]
  [1342] Pride and Prejudice [en]
```

### `--force`

Re-download even when the target file exists.

### `--no-manifest`

Do not write `manifest.json`.

### `-v`, `-q`

Debug logging / warnings and errors only. Use one or the other.

---

## How lookup works

1. Resolve the book list (Gutendex and/or catalog).
2. Keep books whose author matches (see [Author matching](#author-matching)).
3. Apply `--lang`.
4. Deduplicate by Gutenberg ID and sort by numeric ID.
5. Either print (`--list-only`) or download into `--out`.

In `auto` mode Gutendex is tried first (5 attempts with backoff). If it stays down, the official catalog is used. With `--catalog` the API is never contacted.

---

## Download formats and URL fallbacks

Each book has a list of candidate URLs, tried in order. A candidate that fails (404, an HTML error page instead of an ebook, an empty response, a server error after retries) moves on to the next one. The book only counts as failed when every candidate has failed.

In Gutendex mode the URL from the API is tried first, followed by the same list below.

### `txt`

1. `https://www.gutenberg.org/ebooks/{id}.txt.utf-8` (the link on the book's page)
2. `https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt`
3. `https://www.gutenberg.org/files/{id}/{id}-0.txt`
4. `https://www.gutenberg.org/files/{id}/{id}.txt`

### `epub`

1. `https://www.gutenberg.org/ebooks/{id}.epub3.images`
2. `https://www.gutenberg.org/ebooks/{id}.epub.images`
3. `https://www.gutenberg.org/ebooks/{id}.epub.noimages`
4. `https://www.gutenberg.org/cache/epub/{id}/pg{id}.epub`

### `html`

1. `https://www.gutenberg.org/cache/epub/{id}/pg{id}-images.html`
2. `https://www.gutenberg.org/files/{id}/{id}-h/{id}-h.htm`

### `kindle`

1. `https://www.gutenberg.org/ebooks/{id}.kf8.images`
2. `https://www.gutenberg.org/ebooks/{id}.kindle.images`
3. `https://www.gutenberg.org/ebooks/{id}.kindle.noimages`

---

## Output files

```text
Jane_Austen/                          # --out or derived name
  105 - Persuasion.txt
  141 - Mansfield Park.txt
  1342 - Pride and Prejudice.txt
  manifest.json
```

Filenames are `{id} - {sanitized_title}.{ext}`. The Gutenberg ID is always included, so equal titles never overwrite each other. Unsafe characters are removed and the title is capped at about 120 characters. Existing files are skipped unless `--force` is used.

### `manifest.json`

Written at the end of a download run (unless `--no-manifest`), including after Ctrl+C.

| Field | Meaning |
|-------|---------|
| `generated` | UTC timestamp |
| `books[].id` | Gutenberg ID |
| `books[].title` | Title |
| `books[].authors` | Author list from the catalog/API |
| `books[].languages` | Language codes |
| `books[].url` | First candidate URL |
| `books[].alt_urls` | Remaining fallback URLs |
| `books[].status` | `ok`, `skipped`, `no_url`, `failed`, `cancelled` or `not_attempted` |
| `books[].filename` | Base name without extension |

---

## Behaviour notes

| Topic | Behaviour |
|-------|-----------|
| **Retries** | Up to 5 attempts (3 for file downloads) with exponential backoff, only for connection errors, timeouts and `429`/`5xx`. A `Retry-After` header is honoured. A `404` is not retried; the next URL is tried instead. |
| **Pacing** | One shared limiter keeps requests to gutenberg.org at least `--delay` seconds apart, whatever `--workers` is. |
| **Validation** | EPUBs must be zip files; a Kindle file that is really HTML is rejected; an empty response is a failure. |
| **Size notes** | Gutenberg's `Content-Length` is often wrong; a mismatch is noted and the file is kept. |
| **Ctrl+C** | In-flight transfers stop quickly, partial `.part` files are removed, a manifest is written, exit code is **130**. |
| **Deduplication** | The same Gutenberg ID appears only once per run. |

---

## Examples

```bash
# Help
uv run gutenberg_author_download.py --help

# List English Jane Austen texts
uv run gutenberg_author_download.py "Jane Austen" --lang en --list-only

# Download TXT using a local catalog
uv run gutenberg_author_download.py "Doyle, Arthur Conan, 1859-1930" \
  --catalog ./pg_catalog.csv --lang en

# Only books where he is the sole listed author
uv run gutenberg_author_download.py "Doyle, Arthur Conan, 1859-1930" \
  --catalog ./pg_catalog.csv --lang en --sole-author --list-only

# EPUB with images
uv run gutenberg_author_download.py "Jane Austen" \
  --format epub --lang en --out Austen_EPUB

# Include anthologies where the author is only a contributor
uv run gutenberg_author_download.py "Kipling, Rudyard" --include-contributors --list-only

# Force re-download
uv run gutenberg_author_download.py "Poe, Edgar Allan" --format epub --force --out Poe_EPUB

# Catalog only, quiet
uv run gutenberg_author_download.py "Dickens, Charles" \
  --source catalog --catalog ./pg_catalog.csv --lang en -q
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Gutendex returns `503` | The free public API is overloaded | Nothing; the script retries, then falls back to the catalog. Use `--catalog` to skip the API. |
| More books than the website shows | Several editions per title, and co-credited books | Use `--lang en`, `--sole-author`, and a full name with dates |
| Fewer books than expected | Contributor-only credits are excluded by default | Add `--include-contributors` |
| `Catalog file not found` | Bad `--catalog` path | Fix the path (`~` is supported) |
| `Catalog does not look like pg_catalog.csv` | Wrong or truncated file | Re-download the official CSV |
| `No books found` | Name mismatch or too strict a filter | Try the surname only, check `--lang`, or add `--include-contributors` |
| `failed ... HTTP 404 (3 URL(s) tried ...)` | The format doesn't exist for that book | Normal for a few IDs; try another `--format` |
| `size note: got N vs header M` | Unreliable Content-Length | Informational; the file was saved |
| Second run downloads everything again | Different default `--out` (author string differed) | Pass the same `--out DIR` |
| `--delay ... below the 2s` warning | Delay set under 2 seconds | Use 2 or higher |

---

## Policy

Keep the default delay and a low `--workers` value. See [Robot Access to Archive](https://www.gutenberg.org/policy/robot_access.html) and the [Project Gutenberg License](https://www.gutenberg.org/policy/license.html).
