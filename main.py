# Importing necessary libraries
import ast
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import math
from tkinter.filedialog import asksaveasfile
from ebooklib import epub
import re
from progress.bar import Bar
import time
import os
import shutil
import zipfile
import pdfkit

# The site fronts every request with a Cloudflare Turnstile bot check that a
# plain HTTP client can never pass (it needs JS + a canvas fingerprint). A real
# (even headless) browser clears it automatically, so we drive one persistent
# browser session for every page fetch instead of urllib/requests.
playwright = sync_playwright().start()
browser = playwright.chromium.launch(headless=True)
browser_context = browser.new_context(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
page = browser_context.new_page()


def fetch_soup(url):
    # "networkidle" times out here (the site keeps a background connection
    # open); domcontentloaded + a short pause is enough for the Turnstile
    # check and page script to finish.
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    return BeautifulSoup(page.content(), "html.parser")


# Requesting homepage of novel
URL = input("Enter URL : ")
print()

# Parsing using BeautifulSoup
soup = fetch_soup(URL)

soup.style.decompose()

# Extracting chapter count
chapter_num = soup.find(
    "li", title="Glossary + illustrations + division of chapters, etc.")
chapter_count = chapter_num.find("span", class_="grey").text
chapter_count = re.findall(r'\d+', chapter_count)
count = int(chapter_count[0])
print("Available chapters : ", count)
print()

# Extracting index page
redirect = soup.find("a", class_="uppercase bold")['href']
index_page = "https://ranobes.net" + redirect


# Extracting chapter list
i = 1

chapter_list = []
index_list = []
index_list.append(index_page)

#print("Extracting chapter list...")
bar = Bar('Extracting chapter list...', max=int(math.ceil(count / 25)))

while i <= int(math.ceil(count / 25)):

    try:
        page_num = index_page + "page/{num}/" .format(num=i)
        index_list.append(page_num)

        index_soup = fetch_soup(page_num)

        chap_block = index_soup.find("div", id="dle-content")
        chap_block = chap_block.find("script")
        m = re.search(r"\"chapters\":(.*?),\"pages", chap_block.string)
        lst = m.group(1).strip('][').split('},')

        for item in range(0, len(lst)):
            if item != len(lst) - 1:
                lst[item] += '}'
            data = ast.literal_eval(lst[item])

            for key, value in data.items():
                if key == 'link':
                    chapter_list.append(value)
        bar.next()

    except Exception as e:
        print(e)
        print("Retrying after 15 seconds...")
        time.sleep(15)
        continue

    i += 1

bar.finish()
print()

print("Done")
print()

chapter_list = [x for x in chapter_list if x not in index_list]
chapter_list.reverse()


# Creating EpubBook
book = epub.EpubBook()

# Adding metadata
book.add_author(str(soup.find("span", class_="tag_list").text.strip()))

for span in soup("span"):
    span.decompose()

# img_URL = "https://ranobes.net" + \
    # soup.find("img", alt=str(soup.find(class_="poster").text.strip()))["src"]
# img_req = Request(img_URL, headers=headers)
f = open("cover.jpg", 'wb')
# f.write(urlopen(img_req).read())
f.close()
book.set_cover("image.jpg", open("cover.jpg", 'rb').read())
os.remove("cover.jpg")

book.set_title(str(soup.find(class_="title").text.strip()))
book.set_language('en')

chapter_epub = []

# Nunito Sans Font
font = "<link href='https://fonts.googleapis.com/css?family=Nunito Sans' rel='stylesheet'>"

# Extracting introduction to novel
descr = epub.EpubHtml(title='Introduction', file_name='intro.xhtml', lang='en')
descr_novel = soup.find("div", class_="cont-text showcont-h")
descr_para = '''{font}<style>h2{{font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6}}</style>
    <h2>Introduction</h2>''' .format(font=font)
descr_para += '''{font}<style>body{{font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6}}</style>
    <body>{descr_novel}</body>''' .format(font=font, descr_novel=descr_novel)
descr.content = descr_para
book.add_item(descr)


# Extracting chapters
start = input("Enter starting chapter number : ")
end = input("Enter ending chapter number : ")
print()
print("Extracting chapters...")
print()

i = int(start) - 1
while i < int(end):

    try:

        # Requesting chapter page
        site = chapter_list[i]
        site_soup = fetch_soup(site)
        for span in site_soup("span"):
            span.decompose()
        site_soup.find("div", class_="category grey ellipses").decompose()

        # Extracting chapter title
        chapter_title = site_soup.find("h1", class_="h4 title")
        file_name = 'ch{numb}.xhtml' .format(numb=str(i+1))
        c = epub.EpubHtml(
            title=str(chapter_title.text.strip()), file_name=file_name)

        # Extracting chapter content
        chapter_content = site_soup.find_all("p")
        para = '''{font}<h2 style="font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6">
            {title_page}</h2>''' .format(font=font, title_page=str(chapter_title.text.strip()))
        for j in chapter_content:
            para += '''{font}<p style="font-family:'Nunito Sans'; padding:0 10px; line-height: 1.6">{element}
                </p>''' .format(font=font, element=str(j.text.strip()))
        c.content = para

        # Adding chapters to book
        book.add_item(c)
        chapter_epub.append(c)

        # Printing progress on terminal
        print(chapter_title.text.strip())

    except Exception as e:
        print(e)
        print("Retrying after 15 seconds...")
        time.sleep(15)
        continue

    i += 1

print()

# Done fetching pages
browser.close()
playwright.stop()

# Defining index of book
book.toc = (epub.Link('intro.xhtml', 'Introduction', 'intro'),
            (epub.Section('Chapters'),
             (chapter_epub))
            )

# Adding navigation files
book.add_item(epub.EpubNcx())
book.add_item(epub.EpubNav())

book.spine = chapter_epub

# Creating epub file
files = [('EPUB File', '*.epub')]
file_name = asksaveasfile(filetypes=files, defaultextension='*.epub')
os.chdir(os.path.dirname(file_name.name))
file_name = os.path.basename(file_name.name)

epub.write_epub(file_name, book, {})

print("Enjoy your read...")
