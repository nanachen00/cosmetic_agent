import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0"}

for url in [
    "https://www.fda.gov.tw/TC/siteContent.aspx?sid=3218",
    "https://www.fda.gov.tw/TC/siteContent.aspx?sid=3219",
]:
    resp = requests.get(url, headers=headers, timeout=15)
    soup = BeautifulSoup(resp.text, 'html.parser')

    divs = soup.find_all('div', class_=True)
    classes = set()
    for d in divs:
        for c in d.get('class', []):
            classes.add(c)

    print(f"\nURL: {url}")
    print("div classes:", classes)

    body = soup.find('body')
    if body:
        text = body.get_text(separator='\n', strip=True)
        lines = [l for l in text.split('\n') if len(l.strip()) > 20]
        print(f"有效行數: {len(lines)}")
        print("前5行:")
        for l in lines[:5]:
            print(f"  {l[:80]}")