"""
Quick test to inspect MSHSL school page structure.
Run in PyCharm — prints the logo img src and page title.
"""

import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mshsl.org/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def inspect_school(slug: str):
    url = f"https://www.mshsl.org/schools/{slug}"
    print(f"\n--- {url} ---")
    r = SESSION.get(url, timeout=15)
    print(f"Status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return

    soup = BeautifulSoup(r.text, "html.parser")
    print(f"Title: {soup.title.string if soup.title else 'N/A'}")

    # Look for any <img> that might be a logo
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "")
        cls = " ".join(img.get("class", []))
        if any(kw in (src + alt + cls).lower() for kw in ["logo", "school", "mascot", "crest"]):
            print(f"  LOGO IMG: src={src!r}  alt={alt!r}  class={cls!r}")

    # Also print all img srcs so we can spot the pattern
    print("\n  All imgs:")
    for img in soup.find_all("img"):
        print(f"    src={img.get('src','')!r}  alt={img.get('alt','')!r}")


def inspect_listing():
    url = "https://www.mshsl.org/schools"
    print(f"\n--- {url} ---")
    r = SESSION.get(url, timeout=15)
    print(f"Status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return

    soup = BeautifulSoup(r.text, "html.parser")
    print(f"Title: {soup.title.string if soup.title else 'N/A'}")

    # Look for school links and any imgs nearby (logo on listing row?)
    school_links = [
        a for a in soup.find_all("a", href=True)
        if re.match(r"^/schools/[^/?#]+$", a["href"])
    ]
    print(f"\n  Found {len(school_links)} /schools/ links")
    print("\n  First 10 entries — link + any nearby img:")
    for a in school_links[:10]:
        slug = a["href"].split("/schools/")[1]
        # Check parent elements up to 3 levels for an img
        img_src = ""
        node = a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            img = node.find("img")
            if img:
                img_src = img.get("src", "")
                break
        print(f"    {slug:<45}  img={img_src!r}")

    # Pagination: look for next-page links
    next_links = [a["href"] for a in soup.find_all("a", href=True) if "page=" in a["href"]]
    print(f"\n  Pagination links found: {next_links[:5]}")


if __name__ == "__main__":
    inspect_school("united-south-central-high-school")
    inspect_listing()
