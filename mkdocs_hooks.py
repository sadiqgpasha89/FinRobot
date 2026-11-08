"""MkDocs hooks: keep editing notes that belong to the repository copy of a page out of the published site."""
import re


def on_page_markdown(markdown, page, config, files):
    # the Medium draft starts with a "Publishing notes" box for the author; it is not part of the article
    return re.sub(r"> \*\*Publishing notes.*?\n\n", "", markdown, flags=re.S)
