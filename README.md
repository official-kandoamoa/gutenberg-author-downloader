# Gutenberg Downloader

Download books from Project Gutenberg as text, EPUB, Kindle, HTML, audio, or anything else a book page offers. Pick books by **author**, **title**, **ID** or **category**, one or many.

```bash
uv run gutenberg_author_download.py "Jane Austen"
uv run gutenberg_author_download.py --id 5148,1342 --format epub
uv run gutenberg_author_download.py "Doyle, Arthur Conan" --format audio
uv run gutenberg_author_download.py --category "Detective" --lang en --limit 20 --format epub
```

**Requirements:** Python 3.10+, `requests`  
**Run with uv:** `uv run gutenberg_author_download.py ...` (installs `requests` automatically)  
**Run with pip:** `pip install requests && python gutenberg_author_download.py ...`

> **Please read the [Robot policy](#robot-policy) section before large downloads.** Project Gutenberg says its website is for human users and may block the IP address of automated tools that fall outside its mirror and harvest options.

---

## Table of contents

1. [All arguments and options](#all-arguments-and-options)
2. [Choosing books](#choosing-books)
3. [Choosing formats](#choosing-formats)
4. [How lookup works](#how-lookup-works)
5. [Output files](#output-files)
6. [Behaviour notes](#behaviour-notes)
7. [Examples](#examples)
8. [Troubleshooting](#troubleshooting)
9. [Upgrading from the previous version](#upgrading-from-the-previous-version)
10. [Robot policy](#robot-policy)

---

## All arguments and options

```text
gutenberg_author_download.py
    [AUTHOR ...]
    [--id IDS]  [--title TEXT]  [--category TEXT]
    [--list-categories [TEXT]]
    [--format LIST]  [--first-available]
    [--type LIST]  [--lang CODES]  [--limit N]
    [--out DIR]  [--delay SEC]  [--workers N]
    [--source {auto,catalog,gutendex}]  [--catalog PATH]
    [--include-contributors]  [--sole-author]
    [--list-only]  [--force]  [--no-manifest]
    [-v | --verbose]  [-q | --quiet]  [-h | --help]
```

| Name | Repeatable | Default | Summary |
|------|-----------|---------|---------|
| `AUTHOR` (positional) | yes (several names) | - | Author name(s) |
| `--id IDS` | yes | - | Gutenberg ID(s): `5148`, `5148,1342`, range `100-120` |
| `--title TEXT` | yes | - | Title; every word must appear |
| `--category TEXT` | yes | - | Text inside a category (bookshelf) name or subject |
| `--list-categories [TEXT]` | no | - | List category names (optionally filtered) and exit |
| `--format LIST` | no | `txt` (`audio` for `--type sound`) | Comma-separated formats, see [Choosing formats](#choosing-formats) |
| `--first-available` | no | off | Per book, stop after the first listed format that exists |
| `--type LIST` | no | `text` (+`sound` for audio formats) | Catalog item types to search |
| `--lang CODES` | no | all | Language filter, e.g. `en` or `en,fr` |
| `--limit N` | no | none | Only the first N matches (lowest IDs first) |
| `--out DIR` | no | see [Output files](#output-files) | Output folder |
| `--delay SEC` | no | `2.0` | Minimum seconds between requests to gutenberg.org |
| `--workers N` | no | `1` | Parallel transfers |
| `--source` | no | `auto` | `auto` (catalog, then Gutendex), `catalog`, `gutendex` |
| `--catalog PATH` | no | - | Local `pg_catalog.csv` to use |
| `--include-contributors` | no | off | Also match `[Contributor]`/`[Illustrator]`/`[Editor]`/`[Translator]` credits |
| `--sole-author` | no | off | Only books with exactly one credited author |
| `--list-only` | no | off | List matches, download nothing |
| `--force` | no | off | Re-download existing files |
| `--no-manifest` | no | off | Don't write `manifest.json` |
| `-v` / `-q` | no | - | Debug logging / warnings only |

At least one of an author, `--id`, `--title` or `--category` is required (or `--list-categories`).

---

## Choosing books

### Selectors

| Selector | How it matches |
|----------|----------------|
| **Author** (positional) | Every word of the name must appear in one credited name. Case, accents, punctuation and years are ignored: `"Emile Zola"` finds `Zola, Émile, 1840-1902`; `"H. G. Wells"` finds `Wells, H. G. (Herbert George)`. |
| **`--id`** | Exact Gutenberg IDs. Accepts `5148`, `5148,1342`, ranges `100-120`, and may be repeated. IDs not found are reported. |
| **`--title`** | Every word of the text must appear in the title (any order). `--title "Pride Prejudice"` matches *Pride and Prejudice*. |
| **`--category`** | Case-insensitive text found inside a bookshelf name (e.g. `Category: Novels`, `Detective`) **or** a subject heading (e.g. `Boxing -- England -- Fiction`). |

Pass several authors as separate quoted arguments, and repeat `--title`, `--category` or `--id` for several of those. Titles and categories can contain commas, so they are **not** split on commas; IDs and formats are.

### Combining selectors

- **Different selectors narrow each other (AND).** `"Doyle, Arthur Conan" --category Adventure --lang en` = Doyle's books in Adventure.
- **Several values of the same selector add up (OR).** `--title Emma --title Persuasion` = both.
- `--lang` and `--type` also narrow the result.

Want the union of two different selectors (say book 5148 **and** everything by Austen)? Run the command twice into the same `--out` folder.

### `--list-categories [TEXT]`

Prints category (bookshelf) names with the number of books in each, then exits.

```bash
uv run gutenberg_author_download.py --list-categories            # all categories
uv run gutenberg_author_download.py --list-categories science    # only names containing "science"
```

`--category` also searches subject headings, so it can match more than the names listed here.

### `--type LIST`

The catalog contains more than books. Types: `text`, `sound`, `image`, `movingimage`, `dataset`, `collection`, or `any`. `audio` is accepted for `sound` and `video` for `movingimage`.

By default only `text` items are searched. Asking for an audio format automatically adds `sound`. If you only ask for audio formats, only `sound` items are searched. Set `--type` yourself to override.

### Contributor and co-author handling

The catalog tags secondary credits, for example `Doyle, Arthur Conan, 1859-1930 [Contributor]`.

| Mode | Behaviour |
|------|-----------|
| default | Only primary credits match (no `[Role]` tag) |
| `--include-contributors` | Also match `[Contributor]`, `[Illustrator]`, `[Editor]`, `[Translator]` credits |
| `--sole-author` | Book must list exactly one author entry (no co-authors, no illustrator, no editor) |

Example, `"Doyle, Arthur Conan, 1859-1930" --lang en` on one catalog snapshot: `--sole-author` 80 books, default 84, `--include-contributors` 90. Counts on the website can differ because one title often exists as several editions with different IDs, and each ID counts separately.

### Audiobooks have their own IDs

Gutenberg's audiobooks are separate catalog entries (type `sound`) with their own IDs, not extra files on the text book. For example *The Poison Belt* is ID 126 as text and ID 8613 as audio. So `"Doyle, Arthur Conan" --format audio` finds Doyle's audiobook entries (38 in one snapshot), while `--id 126 --format audio` finds nothing.

**One recording, several encodings.** An audiobook page usually offers the same recording in several encodings. *Childhood* by Tolstoy (ID 19680) has 17 chapters, each as `mp3`, `ogg`, `m4b` and `spx`: 68 audio files for one book. `--format audio` takes just one encoding per book (mp3 if offered, otherwise ogg, m4b, opus, spx, ...), so you get the 17 MP3 chapters. To choose an encoding yourself, name it: `--format ogg`, `--format m4b`, `--format spx`. To get several, list them (`--format mp3,ogg`) or use `--format all` (every file on the page, including the transcript and index page).

### `--limit N`

Takes the first N matches in ID order. Handy for testing a big selection.

---

## Choosing formats

`--format` takes a comma-separated list. **Every listed format is downloaded for every book** (saved side by side), unless you add `--first-available`.

| Token | What you get | Saved as |
|-------|--------------|----------|
| `txt` (alias `text`) | Plain text, UTF-8 | `{id} - {title}.txt` |
| `epub` | EPUB3 with images (falls back to older EPUBs) | `{id} - {title}.epub` |
| `html` | Single HTML page | `{id} - {title}.html` |
| `htmlzip` | HTML plus its images, zipped | `{id} - {title}.zip` |
| `kindle` (aliases `mobi`, `azw3`) | Kindle file | `{id} - {title}.mobi` |
| `audio` (aliases `sound`, `audiobook`) | The audio files of an audiobook, **one encoding** (mp3 if offered, else ogg, m4b, opus, spx, ...) | folder `{id} - {title}/` |
| `any` | The first available of `epub, txt, html, kindle, audio` | as above |
| `all` (alias `everything`) | Every file on the book's page | folder `{id} - {title}/` |
| extension, e.g. `mp3`, `ogg`, `m4b`, `pdf`, `zip` | Only files with that extension | folder `{id} - {title}/` |

Any other 2-5 letter token is treated as a file extension. Check your spelling: a typo such as `epb` would silently be treated as an extension and find nothing (an info line is printed).

### `--first-available`

For each book, formats are tried in the order listed and it stops after the first one that exists.

```bash
# EPUB if the book has one, otherwise text, otherwise HTML
uv run gutenberg_author_download.py "Jane Austen" --format epub,txt,html --first-available
```

`--format any` implies it.

### Which formats apply to which items

- Text formats (`txt`, `epub`, `html`, `htmlzip`, `kindle`) apply to non-sound items.
- Audio formats (`audio`, `mp3`, `ogg`, ...) apply to `sound` items only.
- `all` and other extensions apply to everything.

A format that does not apply to an item is skipped quietly, so `--format epub,audio` works on a mixed selection.

### "If available"

A format a book does not have is reported as **not available** and is not an error (exit code stays 0). Only real problems (network errors, bad files) count as failures.

### Where the URLs come from

For `txt`, `epub`, `html`, `htmlzip` and `kindle` the URLs are built from the book ID and tried in order; the first that works wins, and a 404 moves on to the next.

**txt**
1. `https://www.gutenberg.org/ebooks/{id}.txt.utf-8`
2. `https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt`
3. `https://www.gutenberg.org/files/{id}/{id}-0.txt`
4. `https://www.gutenberg.org/files/{id}/{id}.txt`

**epub**
1. `https://www.gutenberg.org/ebooks/{id}.epub3.images`
2. `https://www.gutenberg.org/ebooks/{id}.epub.images`
3. `https://www.gutenberg.org/ebooks/{id}.epub.noimages`
4. `https://www.gutenberg.org/cache/epub/{id}/pg{id}.epub`

**html**
1. `https://www.gutenberg.org/cache/epub/{id}/pg{id}-images.html`
2. `https://www.gutenberg.org/files/{id}/{id}-h/{id}-h.htm`

**htmlzip**
1. `https://www.gutenberg.org/cache/epub/{id}/pg{id}-h.zip`
2. `https://www.gutenberg.org/files/{id}/{id}-h.zip`

**kindle**
1. `https://www.gutenberg.org/ebooks/{id}.kf8.images`
2. `https://www.gutenberg.org/ebooks/{id}.kindle.images`
3. `https://www.gutenberg.org/ebooks/{id}.kindle.noimages`

The single `.html` page usually loads its pictures from a separate folder, so images may be missing; use `htmlzip` if you want them.

`audio`, `all` and extension tokens read the book's page (`https://www.gutenberg.org/ebooks/{id}`) to learn which files exist. This costs **one extra request per book**. Links to send-to-cloud services, metadata (`.rdf`, `.opds`), folder listings and other websites are ignored. Files linked as `/ebooks/{id}.<format>` are saved with the format in the name, e.g. `5148.epub3.images.epub`.

---

## How lookup works

1. The book list comes from the official catalog (`pg_catalog.csv`): your `--catalog` file, or a copy cached at `~/.cache/gutenberg/pg_catalog.csv` and refreshed after 7 days. If a refresh fails but an older cached copy exists, that copy is used.
2. If there is no catalog at all and it cannot be downloaded, **Gutendex** (`gutendex.com`) is used as a fallback.
3. Selectors, `--lang`, `--type` and `--limit` are applied; results are deduplicated by ID and sorted by ID.
4. Either listed (`--list-only`) or downloaded.

| `--source` | Behaviour |
|------------|-----------|
| `auto` | Catalog first, Gutendex if the catalog is unavailable. With `--catalog`, catalog only. |
| `catalog` | Catalog only. |
| `gutendex` | Gutendex only. |

**Gutendex limits:** text items only (no audio), and it needs an internet connection and the public server to be up. It supports all selectors; `--id` is sent as `ids`, category as `topic`, authors and titles as `search`, and the same matching rules are then applied to the results. `--catalog` cannot be combined with `--source gutendex`.

The catalog is complete and works offline once downloaded, which is why it is now the default.

---

## Output files

```text
Doyle_Arthur_Conan/                       # --out, or the author name if exactly one author
  126 - The Poison Belt.epub              # one file per text format
  126 - The Poison Belt.txt
  8613 - The Poison Belt/                 # audio / all / extension formats: one folder per book
    8613-000.mp3
    8613-001.mp3
  manifest.json
```

- **Default folder:** the author name (spaces become `_`, commas dropped) when exactly one author is given, otherwise `gutenberg_books/`.
- **Names:** the Gutenberg ID is always part of the name, so equal titles never collide. Unsafe characters are removed and names are cut to a safe length in **bytes**, so titles in any script work.
- **Existing files are skipped** (no network request for the text formats). For `audio`/`all`/extension formats the book page is fetched once to learn the file names, then existing files are skipped. Use `--force` to re-download.
- **Formats a book lacks are asked about again on every run** (1-4 requests each) since there is no way to know without trying.

### `manifest.json`

Written at the end of a run (unless `--no-manifest`), including after Ctrl+C.

| Field | Meaning |
|-------|---------|
| `generated` | UTC timestamp |
| `books[].id`, `title`, `type`, `authors`, `languages` | Catalog data |
| `books[].attempted` | `false` if the run stopped before reaching this book |
| `books[].files[].format` | The format token that produced the file |
| `books[].files[].status` | `ok`, `skipped`, `unavailable`, `failed`, `cancelled`, or `na` (format doesn't apply to this item type) |
| `books[].files[].message` | Size, reason or error |
| `books[].files[].path` | Path relative to the output folder |
| `books[].files[].url` | URL that worked (or the last one tried) |

---

## Behaviour notes

| Topic | Behaviour |
|-------|-----------|
| **Pacing** | One shared limiter keeps requests to gutenberg.org at least `--delay` seconds apart, whatever `--workers` is. Retries and fallback URLs count too. A delay below 2 seconds prints a warning. |
| **Workers** | Extra workers only overlap slow transfers (e.g. big audio files); they do not raise the request rate. |
| **Retries** | Connection errors, timeouts and `429`/`5xx` are retried with growing waits (5 tries for lookups and book pages, 3 for file downloads; `Retry-After` honoured). A `404` is **not** retried; the next URL is tried. |
| **Interrupted transfers** | A connection that drops mid-download, or a file shorter than the server announced, is detected and retried (3 tries) instead of keeping a damaged file. |
| **Validation** | EPUB and zip files must start with the zip signature; an HTML error page saved in place of a book, audio or text file is rejected. |
| **Ctrl+C** | Transfers stop within moments, partial `.part` files are removed, a manifest is written, exit code **130**. |
| **Exit codes** | `0` success (including "not available"), `1` at least one file failed, `2` bad arguments, `130` interrupted. |
| **Large jobs** | A note about the minimum run time is shown for big selections. Use `--limit` to test first. |
| **Robot policy** | A 2-second delay does not make direct downloads compliant; see [Robot policy](#robot-policy). `--format all`, audio and many formats make many requests. |

---

## Examples

```bash
# Help
uv run gutenberg_author_download.py --help

# See what an author query matches, without downloading
uv run gutenberg_author_download.py "Jane Austen" --lang en --list-only

# Everything by two authors, English, as EPUB
uv run gutenberg_author_download.py "Jane Austen" "Mark Twain" --lang en --format epub

# Specific books by ID (single, list and range), as text and EPUB
uv run gutenberg_author_download.py --id 5148 --id 1342,158 --id 100-105 --format txt,epub

# Specific books by title
uv run gutenberg_author_download.py --title "Pride and Prejudice" --title "Sherlock Holmes" --lang en

# A category, first 20 books, EPUB if there is one, else text
uv run gutenberg_author_download.py --category "Detective" --lang en --limit 20 \
  --format epub,txt --first-available

# Browse categories, then use one
uv run gutenberg_author_download.py --list-categories poetry
uv run gutenberg_author_download.py --category "Category: Poetry" --lang en --limit 10

# Audiobooks by an author (these are separate catalog entries; one encoding per book)
uv run gutenberg_author_download.py "Doyle, Arthur Conan" --format audio

# The OGG encoding instead of MP3
uv run gutenberg_author_download.py --id 19680 --format ogg

# Text and audio together for one author
uv run gutenberg_author_download.py "Doyle, Arthur Conan" --lang en --format epub,audio

# Only the MP3 files of one audiobook
uv run gutenberg_author_download.py --id 8613 --format mp3

# Everything the book page offers for one book
uv run gutenberg_author_download.py --id 5148 --format all

# Best single format per book, whatever it is
uv run gutenberg_author_download.py "Jane Austen" --format any

# Only books where he is the sole listed author, from a local catalog
uv run gutenberg_author_download.py "Doyle, Arthur Conan, 1859-1930" \
  --catalog ./pg_catalog.csv --lang en --sole-author --list-only

# Include anthologies where the author is only a contributor
uv run gutenberg_author_download.py "Kipling, Rudyard" --include-contributors --list-only

# Re-download everything
uv run gutenberg_author_download.py "Poe, Edgar Allan" --format epub --force --out Poe_EPUB
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `choose what to download` | No author, `--id`, `--title` or `--category` given | Add one (`--type`, `--lang` alone are not enough) |
| `No books found` | Name mismatch, too many filters, or wrong type | Try the surname only, drop filters, add `--include-contributors` or `--type any` |
| Audio format finds nothing for a text ID | Audiobooks have their own IDs | Search by author or title with `--format audio`, or use the audiobook's ID |
| `none of the requested formats apply to Text items` | e.g. `--format audio --type text` | Use `--type sound`, or a text format |
| More books than the website shows | Several editions per title, co-credited books | `--lang en`, `--sole-author`, a full name with dates |
| Fewer books than expected | Contributor-only credits are excluded by default | `--include-contributors` |
| `--category` matches too much | It also searches subject headings | Use a more specific text; check with `--list-only` and `--list-categories` |
| `not available (... URL(s) returned 404)` | The book has no file in that format | Normal; try another format or `--first-available` |
| `FAILED: HTTP 403/429` | Rate limit or block | Stop, wait, use a larger `--delay` and `--workers 1` |
| `FAILED: ... returned an HTML page` | Server sent an error page | Retry later |
| `Catalog file not found` / `does not look like pg_catalog.csv` | Wrong path or wrong file | Fix the path, re-download the official CSV |
| Gutendex `503` | The free public API is overloaded | Use the default (catalog) source |
| A `--format` typo does nothing | Unknown 2-5 letter tokens are file extensions | Check the spelling against the format table |
| Second run asks the server again | Missing formats are re-checked; audio/all fetch the book page | Expected; existing files are never re-downloaded |

---

## Upgrading from the previous version

- **Positional author is now optional and repeatable.** Old commands still work.
- **Lookup order changed:** the catalog is now tried first and Gutendex is the fallback (before it was the other way round). `--source gutendex` still forces the API.
- **`--format` takes a list** and has new values (`htmlzip`, `audio`, `any`, `all`, extensions). A single value behaves as before.
- **New:** `--id`, `--title`, `--category`, `--list-categories`, `--type`, `--first-available`, `--limit`.
- **`manifest.json`** now lists every file per book (`files[]`) instead of one `url`/`status` per book; `alt_urls` is gone.
- **`audio` takes one encoding per book** (mp3 preferred) rather than every encoding.
- **Exit code `1`** is now returned when any file failed.
- **Matching fixes:** accented names and initials now match (`Emile Zola`, `H. G. Wells`); names in parentheses, like `(Herbert George)`, now count for matching.

---

## Robot policy

Project Gutenberg's [Robot Access page](https://www.gutenberg.org/policy/robot_access.html) says the website is "intended for human users only" and that any perceived use of automated tools to access it will result in a temporary or permanent block of your IP address. It lists these exceptions:

| Exception | What it is |
|-----------|------------|
| **Mirror** | Set up a private mirror (rsync) to get all files. Best for a full copy. See the [Mirroring How-To](https://www.gutenberg.org/help/mirroring.html). |
| **Harvest page** | `wget -w 2 -m -H "https://www.gutenberg.org/robot/harvest?filetypes[]=html"`. File types: `html`, `txt`, `epub.images`, `epub.noimages`, `kindle.images`, `kindle.noimages`, `mp3`. Add `&langs[]=de` for one language. |
| **Catalog data** | The machine-readable catalog is public domain and meant for tools. |

**What this script does.** Reading the catalog is covered (it downloads `pg_catalog.csv` once a week, or uses your `--catalog` file). The downloads themselves are not: the script requests files straight from `www.gutenberg.org` (`/ebooks/{id}.*`, `/cache/epub/...`, `/files/...`), and for audio, `all` and extension formats it also opens the book page `/ebooks/{id}`. None of that is on the list of exceptions. The 2-second delay is the `-w 2` of the harvest example; it is polite, but it is not permission. There is a real chance of an IP block, mostly with large selections or repeated runs.

**To lower the risk**

- Keep selections small and test with `--limit`.
- Keep `--delay 2` or higher and `--workers 1`.
- Prefer `txt`, `epub`, `html` over `all`, audio or extensions: those need no extra book-page request.
- Don't re-run with `--force` unless you must.
- For whole collections, languages or file types, use a private mirror or the harvest page above instead of this script.

A mirror or harvest mode is not built into the script.

Gutenberg also rebuilds its generated formats monthly and fixes books over time, so a saved copy can go out of date; `--force` refreshes it. See also the [Project Gutenberg License](https://www.gutenberg.org/policy/license.html).
