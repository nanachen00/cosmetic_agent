import requests
from bs4 import BeautifulSoup

headers = {"User-Agent": "Mozilla/5.0"}

# 測試 1：食藥署法規頁（靜態 HTML）
url = "https://www.fda.gov.tw/tc/lawContent.aspx?cid=55&id=794"
resp = requests.get(url, headers=headers, timeout=15)
print(f"Status: {resp.status_code}")
soup = BeautifulSoup(resp.text, 'html.parser')
text = soup.get_text(separator='\n', strip=True)
lines = [l for l in text.split('\n') if len(l.strip()) > 20]
print(f"有效行數: {len(lines)}")
for l in lines[:10]:
    print(f"  {l[:80]}")

# 測試 2：PDF
print("\n\n測試 PDF 下載...")
pdf_url = "https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637111364087109557&type=3&iid=10989"
resp2 = requests.get(pdf_url, headers=headers, timeout=15)
print(f"Status: {resp2.status_code}")
print(f"Content-Type: {resp2.headers.get('Content-Type', '')}")
print(f"Content-Length: {len(resp2.content)} bytes")
if 'pdf' in resp2.headers.get('Content-Type', '').lower():
    with open('/tmp/test_law.pdf', 'wb') as f:
        f.write(resp2.content)
    print("PDF 下載成功，存到 /tmp/test_law.pdf")