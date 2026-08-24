# Importing necessary libraries
import argparse
import ast
import hashlib
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from tkinter.filedialog import asksaveasfile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from ebooklib import epub
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from progress.bar import Bar
import shutil
import zipfile
import pdfkit

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Nunito Sans Font
FONT = "<link href='https://fonts.googleapis.com/css?family=Nunito Sans' rel='stylesheet'>"

CAPTCHA_MARKERS = (
    "checking your browser",
    "just a moment",
    "attention required",
    "verify you are human",
)

MAX_COMMENT_INDENT_DEPTH = 5
COMMENT_PAGE_SAFETY_CAP = 50

DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".page_cache")
DEFAULT_OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name, max_length=150):
    name = _INVALID_FILENAME_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_length] if name else "untitled"


class RunAborted(Exception):
    """Raised when a human explicitly chooses to abort the run."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_ANSI_COLORS = {
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[31m",
}
_ANSI_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        color = _ANSI_COLORS.get(record.levelno)
        if color and sys.stderr.isatty():
            return f"{color}{message}{_ANSI_RESET}"
        return message


def setup_logging(level):
    if os.name == "nt":
        # Enables ANSI escape processing on legacy Windows consoles.
        os.system("")
    # Chapter titles/comment authors can contain characters the console's
    # legacy codepage (e.g. cp1252) can't encode; replace rather than crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter("%(levelname)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])


def log_line(message, progress_bar=None, level=logging.INFO):
    if progress_bar is not None:
        print()
    logging.log(level, message)


# ---------------------------------------------------------------------------
# Page cache
# ---------------------------------------------------------------------------

class PageCache:
    """Caches fetched pages on disk, keyed by URL, so re-running the script
    over the same chapters (e.g. resuming after an anti-flood block) doesn't
    refetch pages it already has. Deliberately NOT used inside BrowserSession
    itself - fetch_with_retry only writes to the cache once it has confirmed
    a page is real content (not a CAPTCHA/challenge page), so a block never
    gets cached and served back as if it were the real page."""

    def __init__(self, cache_dir, enabled=True):
        self.cache_dir = cache_dir
        self.enabled = enabled
        if self.enabled:
            os.makedirs(self.cache_dir, exist_ok=True)

    def _path_for(self, url):
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{key}.html")

    def get(self, url):
        if not self.enabled:
            return None
        path = self._path_for(url)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return BeautifulSoup(f.read(), "html.parser")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            logging.debug(f"Ignoring unreadable cache entry for {url}: {e}")
            return None

    def set(self, url, soup):
        if not self.enabled:
            return
        try:
            with open(self._path_for(url), "w", encoding="utf-8") as f:
                f.write(str(soup))
        except FileNotFoundError:
            # The cache directory itself is missing - it may have been
            # removed after startup (e.g. by an external cleanup). Recreate
            # it and retry once rather than silently disabling caching for
            # the rest of the run.
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(self._path_for(url), "w", encoding="utf-8") as f:
                    f.write(str(soup))
            except OSError as e:
                logging.debug(f"Could not write cache entry for {url}: {e}")
        except OSError as e:
            logging.debug(f"Could not write cache entry for {url}: {e}")


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------

def safe_page_content(page, attempts=5, delay=0.5):
    """page.content() can transiently raise if called while the page is mid-
    navigation (e.g. right after a Turnstile challenge auto-redirects on
    success) - retry briefly instead of propagating that as a hard failure."""
    last_error = None
    for _ in range(attempts):
        try:
            return page.content()
        except PlaywrightError as e:
            last_error = e
            time.sleep(delay)
    raise last_error


class BrowserSession:
    # The site fronts every request with a Cloudflare Turnstile bot check that
    # a plain HTTP client can never pass (it needs JS + a canvas fingerprint).
    # A real (even headless) browser clears it automatically, so we drive one
    # persistent browser session for every page fetch instead of urllib/requests.

    def __init__(self, timeout_ms=30000, wait_ms=1500):
        self.timeout_ms = timeout_ms
        self.wait_ms = wait_ms
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self._launch(headless=True)

    def _launch(self, headless, storage_state=None):
        self.browser = self.playwright.chromium.launch(headless=headless)
        self.context = self.browser.new_context(
            user_agent=USER_AGENT, storage_state=storage_state)
        self.page = self.context.new_page()

    def fetch_soup(self, url):
        # "networkidle" times out here (the site keeps a background connection
        # open); domcontentloaded + a short pause is enough for the Turnstile
        # check and page script to finish.
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        self.page.wait_for_timeout(self.wait_ms)
        return BeautifulSoup(safe_page_content(self.page), "html.parser")

    def switch_to_headed(self, url):
        state = self.context.storage_state()
        self.browser.close()
        self._launch(headless=False, storage_state=state)
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            self.page.bring_to_front()
        except PlaywrightError:
            pass

    def switch_to_headless(self):
        state = self.context.storage_state()
        self.browser.close()
        self._launch(headless=True, storage_state=state)

    def close(self):
        if self.browser is not None:
            self.browser.close()
        self.playwright.stop()


# ---------------------------------------------------------------------------
# Failure classification / anti-flood / human intervention
# ---------------------------------------------------------------------------

def classify_exception(e):
    if isinstance(e, PlaywrightTimeoutError):
        return "the page took too long to respond (timeout)"
    text = str(e)
    if "ERR_INTERNET_DISCONNECTED" in text:
        return "no internet connection"
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "could not resolve ranobes.net (DNS failure or no internet)"
    if "ERR_CONNECTION_REFUSED" in text or "ERR_CONNECTION_RESET" in text:
        return "ranobes.net refused the connection (the site may be down)"
    if "ERR_CONNECTION_TIMED_OUT" in text:
        return "connection to ranobes.net timed out"
    return f"unexpected error: {text.splitlines()[0]}"


def is_anti_flood_blocked(soup):
    marker = soup.select_one(
        ".cf-turnstile, iframe[src*='challenges.cloudflare.com'], "
        "#captcha, [name*='captcha'], .g-recaptcha, .h-captcha")
    if marker is not None:
        return True
    title = soup.title.get_text(strip=True).lower() if soup.title else ""
    body_text = soup.get_text(" ", strip=True).lower()[:500]
    return any(marker in title or marker in body_text for marker in CAPTCHA_MARKERS)


def safe_input(prompt):
    """input() that degrades to None (instead of crashing) if no stdin is
    available - e.g. the script is ever run non-interactively."""
    try:
        return input(prompt)
    except EOFError:
        return None


def handle_anti_flood(session, blocked_url):
    logging.warning("Anti-flood protection triggered by the site.")
    print(f"If a browser window doesn't appear, open this URL manually: {blocked_url}")
    session.switch_to_headed(blocked_url)
    try:
        while True:
            response = safe_input(
                "Solve the verification in the browser window, then press Enter "
                "to continue (or type 'abort'): ")
            if response is None:
                logging.error("No input available - aborting.")
                raise RunAborted("Aborted during anti-flood verification (no input available)")
            if response.strip().lower() == "abort":
                raise RunAborted("Aborted during anti-flood verification")
            try:
                soup = BeautifulSoup(safe_page_content(session.page), "html.parser")
            except PlaywrightError as e:
                logging.warning(f"Could not read the page yet ({classify_exception(e)}) - "
                                 "checking again.")
                continue
            if not is_anti_flood_blocked(soup):
                logging.info("Verification cleared, resuming...")
                return
            logging.warning("Still blocked - please complete the verification.")
    finally:
        session.switch_to_headless()


def prompt_human_intervention(label, url, message):
    print()
    logging.error(f"{label} failed repeatedly: {message}")
    print(f"URL: {url}")
    while True:
        choice = safe_input("[R]etry / [S]kip this item / [A]bort the run? ")
        if choice is None:
            logging.error("No input available - aborting.")
            return "abort"
        choice = choice.strip().lower()
        if choice in ("r", "retry"):
            return "retry"
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("a", "abort"):
            return "abort"
        print("Please enter R, S, or A.")


def fetch_with_retry(session, url, args, *, label, progress_bar=None):
    cached = args.cache.get(url)
    if cached is not None:
        log_line(f"{label}: using cached copy.", progress_bar, logging.INFO)
        return cached

    attempt = 0
    while True:
        try:
            soup = session.fetch_soup(url)
        except KeyboardInterrupt:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            attempt += 1
            message = classify_exception(e)
            log_line(f"{label}: {message} (attempt {attempt}/{args.retries})",
                      progress_bar, logging.WARNING)
            if attempt >= args.retries:
                action = prompt_human_intervention(label, url, message)
                if action == "retry":
                    attempt = 0
                    continue
                if action == "skip":
                    logging.warning(f"Skipping {label} at user's request.")
                    return None
                raise RunAborted(f"Aborted while fetching {label}")
            time.sleep(args.retry_delay)
            continue

        if is_anti_flood_blocked(soup):
            log_line(f"{label}: anti-flood protection triggered.", progress_bar, logging.WARNING)
            handle_anti_flood(session, url)
            continue

        if args.request_delay:
            time.sleep(args.request_delay)
        # Only cache confirmed-good content - never a CAPTCHA/challenge page.
        args.cache.set(url, soup)
        return soup


# ---------------------------------------------------------------------------
# Comment extraction (item 1)
# ---------------------------------------------------------------------------

@dataclass
class CommentNode:
    author: str
    timestamp: str
    reply_to: str
    upvotes: str
    downvotes: str
    body_html: str
    replies: list = field(default_factory=list)


def _text_or(tag, default=""):
    return tag.get_text(strip=True) if tag is not None else default


def _sanitize_node(node):
    # Safe-subset sanitization: keep text, <br> line breaks, and quote blocks
    # (styled distinctly); drop links, images, and every other embedded tag -
    # comment bodies are user-generated and untrusted.
    parts = []
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            parts.append(escape(str(child)))
        elif isinstance(child, Tag):
            if child.name == "br":
                parts.append("<br>")
            elif "quote" in (child.get("class") or []):
                inner = _sanitize_node(child).strip()
                if inner:
                    parts.append(
                        '<div style="margin:.5em 0;padding:.3em .8em;'
                        'border-left:3px solid #999;font-style:italic;'
                        f'opacity:.85">{inner}</div>')
            else:
                parts.append(_sanitize_node(child))
    return "".join(parts)


def _extract_comment_list(ol_tag):
    comments = []
    for li in ol_tag.find_all("li", class_="comments-tree-item", recursive=False):
        comment_div = li.find("div", class_="comment")
        if comment_div is None:
            continue

        author = _text_or(comment_div.select_one(".com_info .com_user .name a"), "Unknown")
        time_tag = comment_div.select_one(".com_info .com_user time")
        timestamp = time_tag.get("datetime", "") if time_tag else ""
        reply_span = comment_div.select_one(
            ".com_info .com_user time span[title^='Answered by user']")
        reply_to = reply_span["title"].replace("Answered by user: ", "") if reply_span else ""
        upvotes = _text_or(comment_div.select_one(".rate [id^='comments-likes-id-']"), "0")
        downvotes = _text_or(comment_div.select_one(".rate [id^='comments-dislikes-id-']"), "0")
        body_tag = comment_div.select_one(".com_content .cont-text div[id^='comm-id-']")
        body_html = _sanitize_node(body_tag).strip() if body_tag is not None else ""

        node = CommentNode(author=author, timestamp=timestamp, reply_to=reply_to,
                            upvotes=upvotes, downvotes=downvotes, body_html=body_html)

        nested_ol = li.find("ol", class_="comments-tree-list", recursive=False)
        if nested_ol is not None:
            node.replies = _extract_comment_list(nested_ol)
        comments.append(node)
    return comments


def extract_comments(site_soup):
    container = site_soup.find("div", id="dle-comments-list")
    if container is None:
        return []
    top_list = container.find("ol", class_="comments-tree-list")
    if top_list is None:
        return []
    return _extract_comment_list(top_list)


def extract_comments_with_pagination(site_soup, session, args, chapter_url):
    comments = extract_comments(site_soup)
    nav = site_soup.find(class_="dle-comments-navigation")
    seen_urls = {chapter_url}
    pages_followed = 0
    # Best-effort: no chapter observed during development had enough comments
    # to trigger pagination, so this path is unverified against a live example.
    while nav is not None and pages_followed < COMMENT_PAGE_SAFETY_CAP:
        next_link = nav.find("a", string=re.compile(r"(next|»|→)", re.I)) \
            or nav.find("a", class_=re.compile("next", re.I))
        if next_link is None or not next_link.get("href"):
            break
        next_url = next_link["href"]
        if next_url.startswith("/"):
            next_url = "https://ranobes.net" + next_url
        if next_url in seen_urls:
            break
        seen_urls.add(next_url)
        next_soup = fetch_with_retry(session, next_url, args, label="comments page")
        if next_soup is None:
            break
        comments.extend(extract_comments(next_soup))
        nav = next_soup.find(class_="dle-comments-navigation")
        pages_followed += 1
    return comments


def _format_timestamp(iso_string):
    if not iso_string:
        return ""
    try:
        return datetime.fromisoformat(iso_string).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso_string


def render_comments_html(comments, depth=0):
    if not comments:
        return ""
    indent_px = min(depth, MAX_COMMENT_INDENT_DEPTH) * 18
    parts = []
    for c in comments:
        reply_line = (f'<div style="font-size:.85em;color:#888">'
                       f'&#8618; replying to {escape(c.reply_to)}</div>') if c.reply_to else ""
        parts.append(f'''
            <div style="margin:10px 0 10px {indent_px}px;padding:8px 12px;
                        border-left:2px solid #ccc;">
                <div style="font-weight:bold">{escape(c.author)}</div>
                <div style="font-size:.8em;color:#888">
                    {escape(_format_timestamp(c.timestamp))} &middot; +{escape(c.upvotes)}/-{escape(c.downvotes)}
                </div>
                {reply_line}
                <div style="margin-top:4px">{c.body_html}</div>
            </div>''')
        if c.replies:
            parts.append(render_comments_html(c.replies, depth + 1))
    return "".join(parts)


def build_comments_section(comments):
    if not comments:
        return ""
    body = render_comments_html(comments)
    return f'''{FONT}<div style="margin-top:2em;padding-top:1em;border-top:2px solid #ccc;
                font-family:'Nunito Sans';line-height:1.6">
        <h2 style="font-family:'Nunito Sans'">Comments</h2>
        {body}
    </div>'''


# ---------------------------------------------------------------------------
# CLI / interactive prompts (item 3)
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape a novel from ranobes.net and save it as an .epub. "
                     "Any option not passed on the command line is prompted for "
                     "interactively, matching the script's original fully-"
                     "interactive behavior.",
    )
    parser.add_argument("--url", help="URL of the novel's page on ranobes.net")
    parser.add_argument("--start", type=int, help="Starting chapter number")
    parser.add_argument("--end", type=int, help="Ending chapter number")

    comments_group = parser.add_mutually_exclusive_group()
    comments_group.add_argument(
        "--comments", dest="comments", action="store_true", default=None,
        help="Include reader comments for each chapter (default: off)")
    comments_group.add_argument(
        "--no-comments", dest="comments", action="store_false",
        help="Do not include reader comments (default)")

    parser.add_argument(
        "--retries", type=int, default=5,
        help="Max retry attempts for a page before asking for human intervention "
             "(default: 5)")
    parser.add_argument(
        "--retry-delay", type=float, default=15,
        help="Seconds to wait between retries (default: 15)")
    parser.add_argument(
        "--request-delay", type=float, default=0,
        help="Seconds to wait between successive page fetches, to help avoid "
             "triggering anti-flood protection (default: 0)")
    parser.add_argument(
        "--timeout", type=int, default=30000,
        help="Page navigation timeout in milliseconds (default: 30000)")
    parser.add_argument(
        "--wait", type=int, default=1500,
        help="Milliseconds to wait after a page loads before reading its "
             "content (default: 1500)")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Terminal log verbosity (default: INFO)")
    parser.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help="Directory to cache fetched pages in, so re-running over the same "
             f"chapters doesn't refetch them (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Fetch every page fresh, ignoring and not writing to the cache "
             "(default: caching is on)")
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        help="Directory the save dialog opens in by default, pre-filled with "
             f"a name based on the novel title and chapter range (default: "
             f"{DEFAULT_OUTPUT_DIR})")
    return parser.parse_args()


def resolve_interactive(args):
    if not args.url:
        args.url = input("Enter URL : ").strip()
    return args


def resolve_chapter_range(args, count):
    if args.start is None:
        args.start = int(input("Enter starting chapter number : "))
    if args.end is None:
        args.end = int(input("Enter ending chapter number : "))
    if args.comments is None:
        response = input("Include chapter comments? [y/N] : ").strip().lower()
        args.comments = response in ("y", "yes")
    return args


# ---------------------------------------------------------------------------
# Assembly / save
# ---------------------------------------------------------------------------

def _chapter_number(item):
    match = re.search(r'(\d+)', item.file_name)
    return int(match.group(1)) if match else None


def build_output_filename(book, chapter_epub):
    numbers = sorted(n for n in (_chapter_number(c) for c in chapter_epub) if n is not None)
    if not numbers:
        chapter_part = "Chapters"
    elif numbers[0] == numbers[-1]:
        chapter_part = f"Chapter {numbers[0]}"
    else:
        chapter_part = f"Chapters {numbers[0]}-{numbers[-1]}"
    title = sanitize_filename(getattr(book, "title", "") or "Untitled Novel")
    return f"{title} - {chapter_part}.epub"


def finalize_and_save(book, chapter_epub, output_dir=DEFAULT_OUTPUT_DIR):
    book.toc = (epub.Link('intro.xhtml', 'Introduction', 'intro'),
                (epub.Section('Chapters'), tuple(chapter_epub)))
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = chapter_epub

    os.makedirs(output_dir, exist_ok=True)
    save_file = asksaveasfile(
        filetypes=[('EPUB File', '*.epub')],
        defaultextension='.epub',
        initialdir=output_dir,
        initialfile=build_output_filename(book, chapter_epub),
    )
    if save_file is None:
        logging.warning("Save cancelled - epub was not written.")
        return
    save_path = save_file.name
    save_file.close()
    epub.write_epub(save_path, book, {})
    logging.info(f"Saved to {save_path}")
    logging.info("Enjoy your read...")


def offer_partial_save(book, chapter_epub, output_dir=DEFAULT_OUTPUT_DIR):
    if not chapter_epub:
        logging.info("No chapters were fetched - nothing to save.")
        return
    logging.info(f"{len(chapter_epub)} chapter(s) fetched before stopping.")
    try:
        response = input("Build the epub with the chapters fetched so far? [y/N]: ").strip().lower()
    except KeyboardInterrupt:
        print()
        return
    except EOFError:
        # No stdin to ask - default to saving rather than silently discarding
        # chapters that were already successfully fetched.
        logging.warning("No input available - saving the chapters fetched so far by default.")
        finalize_and_save(book, chapter_epub, output_dir)
        return
    if response in ("y", "yes"):
        finalize_and_save(book, chapter_epub, output_dir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    setup_logging(getattr(logging, args.log_level))
    args = resolve_interactive(args)

    args.cache = PageCache(args.cache_dir, enabled=not args.no_cache)
    logging.info(f"Page cache: {'disabled (--no-cache)' if args.no_cache else args.cache_dir}")

    session = BrowserSession(timeout_ms=args.timeout, wait_ms=args.wait)
    book = epub.EpubBook()
    chapter_epub = []

    try:
        soup = fetch_with_retry(session, args.url, args, label="novel page")
        if soup is None:
            logging.error("Could not load the novel page. Exiting.")
            return
        soup.style.decompose()

        # Extracting chapter count
        chapter_num = soup.find(
            "li", title="Glossary + illustrations + division of chapters, etc.")
        chapter_count_text = chapter_num.find("span", class_="grey").text
        count = int(re.findall(r'\d+', chapter_count_text)[0])
        logging.info(f"Available chapters : {count}")

        # Extracting index page
        redirect = soup.find("a", class_="uppercase bold")['href']
        index_page = "https://ranobes.net" + redirect

        # Extracting chapter list
        chapter_list = []
        index_list = [index_page]
        pages = int(math.ceil(count / 25))
        bar = Bar('Extracting chapter list...', max=pages)

        for i in range(1, pages + 1):
            page_num = index_page + f"page/{i}/"
            index_list.append(page_num)

            index_soup = fetch_with_retry(
                session, page_num, args, label=f"chapter index page {i}", progress_bar=bar)
            if index_soup is None:
                logging.warning(f"Skipping index page {i} - chapter list may be incomplete.")
                bar.next()
                continue

            chap_block = index_soup.find("div", id="dle-content").find("script")
            m = re.search(r'"chapters":(.*?),"pages', chap_block.string)
            lst = m.group(1).strip('][').split('},')

            for item in range(len(lst)):
                if item != len(lst) - 1:
                    lst[item] += '}'
                data = ast.literal_eval(lst[item])
                for key, value in data.items():
                    if key == 'link':
                        chapter_list.append(value)
            bar.next()

        bar.finish()
        print()

        chapter_list = [x for x in chapter_list if x not in index_list]
        chapter_list.reverse()

        # Creating EpubBook metadata
        book.add_author(str(soup.find("span", class_="tag_list").text.strip()))

        for span in soup("span"):
            span.decompose()

        f = open("cover.jpg", 'wb')
        f.close()
        book.set_cover("image.jpg", open("cover.jpg", 'rb').read())
        os.remove("cover.jpg")

        book.set_title(str(soup.find(class_="title").text.strip()))
        book.set_language('en')

        # Extracting introduction to novel
        descr = epub.EpubHtml(title='Introduction', file_name='intro.xhtml', lang='en')
        descr_novel = soup.find("div", class_="cont-text showcont-h")
        descr_para = '''{font}<style>h2{{font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6}}</style>
            <h2>Introduction</h2>'''.format(font=FONT)
        descr_para += '''{font}<style>body{{font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6}}</style>
            <body>{descr_novel}</body>'''.format(font=FONT, descr_novel=descr_novel)
        descr.content = descr_para
        book.add_item(descr)

        # Extracting chapters
        args = resolve_chapter_range(args, count)
        logging.info("Extracting chapters...")

        i = args.start - 1
        while i < args.end:
            chapter_url = chapter_list[i]
            site_soup = fetch_with_retry(session, chapter_url, args, label=f"chapter {i + 1}")
            if site_soup is None:
                logging.warning(f"Skipped chapter {i + 1} at user's request.")
                i += 1
                continue

            # Must run BEFORE the span-strip below: the reply-to marker and
            # online-status spans used by comment extraction live inside
            # #dle-comments-list, which the span-strip would otherwise erase.
            comments_html = ""
            if args.comments:
                comments = extract_comments_with_pagination(site_soup, session, args, chapter_url)
                comments_html = build_comments_section(comments)
            # Always strip #dle-comments-list before scanning for <p> tags below,
            # even when comments are off - keeps chapter-text extraction (which
            # searches the whole page, not a scoped container) safe from ever
            # picking up stray content from inside the comments tree.
            comments_container = site_soup.find("div", id="dle-comments-list")
            if comments_container is not None:
                comments_container.decompose()

            for span in site_soup("span"):
                span.decompose()
            category_div = site_soup.find("div", class_="category grey ellipses")
            if category_div is not None:
                category_div.decompose()

            # Extracting chapter title
            chapter_title = site_soup.find("h1", class_="h4 title")
            file_name = f'ch{i + 1}.xhtml'
            c = epub.EpubHtml(title=str(chapter_title.text.strip()), file_name=file_name)

            # Extracting chapter content
            chapter_content = site_soup.find_all("p")
            para = '''{font}<h2 style="font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6">
                {title_page}</h2>'''.format(font=FONT, title_page=str(chapter_title.text.strip()))
            for j in chapter_content:
                para += '''{font}<p style="font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6">{element}
                    </p>'''.format(font=FONT, element=str(j.text.strip()))
            if comments_html:
                para += comments_html
            c.content = para

            # Adding chapters to book
            book.add_item(c)
            chapter_epub.append(c)

            # Printing progress on terminal
            logging.info(chapter_title.text.strip())

            i += 1

        finalize_and_save(book, chapter_epub, args.output_dir)

    except RunAborted as e:
        logging.warning(str(e))
        offer_partial_save(book, chapter_epub, args.output_dir)
    except KeyboardInterrupt:
        logging.info("Interrupted by user, shutting down...")
        offer_partial_save(book, chapter_epub, args.output_dir)
    finally:
        session.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Interrupted by user, shutting down...")
