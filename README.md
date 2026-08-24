# Ranobes Scraper

This Python script scrapes novels from [Ranobes](https://ranobes.net/) and saves them into .epub format.

### Features

- Regular updates
- No 'In preparation, Keguan...' for chapters
- 10,000+ novels to scrape
- Terminal based and simple to use
- Scraping progress can be monitored
- Optionally includes each chapter's reader comments (nested replies, quotes) in the epub
- Automatically hands off to a real browser window if the site's anti-flood/CAPTCHA protection kicks in, then resumes on its own
- Caches fetched pages on disk so re-running over the same chapters doesn't refetch them
- Configurable retries, delays, and timeouts; clear terminal logging for common failures (no internet, site down, timeouts)

### Getting Started

Install the latest version of [Python](https://www.python.org/). It may work with older versions but has not been tested.

### Prerequisites

This script depends on bs4, ebooklib, progress, and playwright. To install these navigate to the project folder and run

```
pip install -r requirements.txt
python -m playwright install chromium
```

The second command is a one-time download of the Chromium browser the script drives — the site can't be scraped with plain HTTP requests, since it fronts every page with a bot check that needs a real browser to clear.

### Usage

Navigate to the project folder and run

```
python main.py
```
to start the script. It's fully interactive by default — it'll prompt you for the novel URL, then a starting/ending chapter number, then whether to include comments.

Now copy and paste the URL of the novel you wish to scrape from [Ranobes](https://ranobes.net/).

<img src = "images/Demo/novel_webpage.PNG" alt = "novel_webpage">


It will start extracting the chapter list.

<img src = "images/Demo/extract_index.PNG" alt = "extract_index">


After that, select the chapter range and it will start scraping the chapters.

<img src = "images/Demo/chapter_range.PNG" alt = "chapter_range">


Once done, a save dialog opens pre-filled with a sensible filename (novel title + chapter range) in the project folder — pick a different location if you'd like, or just hit Save.

<img src = "images/Demo/save_file.PNG" alt = "save_file">


Enjoy your read!

### Command-line options

Every prompt above can also be supplied as a flag, which skips that particular prompt — handy for scripting or resuming a specific range without retyping the URL:

```
python main.py --url <novel-url> --start 1 --end 20
python main.py --url <novel-url> --start 1 --end 20 --comments
python main.py --help
```

Run `python main.py --help` for the full list, including:

- `--comments` / `--no-comments` — include reader comments per chapter (off by default)
- `--retries`, `--retry-delay`, `--request-delay`, `--timeout`, `--wait` — tune how patiently the script retries a failing page and how much it paces itself between requests
- `--cache-dir`, `--no-cache` — where fetched pages are cached, or disable caching for a fully fresh run
- `--output-dir` — where the save dialog opens by default
- `--log-level` — terminal verbosity

### Anti-flood / CAPTCHA handling

Under sustained request volume, Ranobes' anti-bot protection can escalate to a real interactive CAPTCHA that no script can solve on its own. When that happens, the script automatically opens a visible browser window (the same session, so your cookies carry over) for you to solve it — just complete the check and press Enter in the terminal to continue. It'll also print the URL in case the window doesn't pop up for some reason.

If a page keeps failing after several retries for another reason (bad connection, site hiccup, etc.), the script stops and asks whether to retry, skip that item, or abort — it never silently gives up on a chapter. If you abort or interrupt the run (Ctrl+C) partway through, it'll offer to save an epub with whatever chapters were already fetched, rather than losing that work.


### Lithium: EPUB Reader

Personally, I prefer the [Lithium: EPUB Reader](https://play.google.com/store/apps/details?id=com.faultexception.reader) due to its simplicity and ease of use to read .epub files.

<table>
    <tr>
        <td><img src = "images/Lithium/lithium_1.png" height = 400 width = 180 alt = "lithium_1"></td>
        <td><img src = "images/Lithium/lithium_2.png" height = 400 width = 180 alt = "lithium_2"></td>
        <td><img src = "images/Lithium/lithium_3.png" height = 400 width = 180 alt = "lithium_3"></td>
        <td><img src = "images/Lithium/lithium_4.png" height = 400 width = 180 alt = "lithium_4"></td>
    </tr>
</table>

### Finally

Feel free to open an issue if you face any bugs or have any suggestions.
