import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 試這幾個靜態網站
urls = [
    # 肌研台灣官網
    "https://www.hadalabo.com.tw/product_list.php",
    # 康是美（跟屈臣氏不同）
    "https://www.cosmed.com.tw/search?q=肌研",
    # 生活市集
    "https://www.ihergo.com/search.php?q=Neogence+洗面乳",
    # 台灣好評網
    "https://www.momoshop.com.tw/search/searchShop.jsp?keyword=Neogence玻尿酸洗面乳",
]

for url in urls:
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"\nURL: {url[:60]}")
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            text = soup.get_text(separator='\n', strip=True)
            lines = [l for l in text.split('\n') if len(l) > 20]
            print(f"有效行數：{len(lines)}")
            print(f"前3行：")
            for l in lines[:3]:
                print(f"  {l[:80]}")
    except Exception as e:
        print(f"  錯誤：{e}")