import fitz

doc = fitz.open('/tmp/test_law.pdf')
print(f"總頁數: {doc.page_count}")

for i in range(min(3, doc.page_count)):
    page = doc[i]
    text = page.get_text()
    print(f"\n=== 第 {i+1} 頁 ===")
    print(text[:500])