"""
Cyberbiz API Client
處理 HMAC 簽名驗證，提供商品資料抓取功能
"""
import hmac
import hashlib
import base64
from datetime import datetime, timezone
from email.utils import formatdate
import requests
from typing import Optional


class CyberbizClient:
    def __init__(self, username: str, secret: str, base_url: str = "https://api.cyberbiz.co"):
        self.username = username
        self.secret = secret
        self.base_url = base_url

    def _get_x_date(self) -> str:
        """產生 GMT 格式的日期字串"""
        return formatdate(usegmt=True)

    def _make_signature(self, method: str, path: str, x_date: str, body: str = "") -> tuple:
        """
        產生 HMAC 簽名

        Returns:
            (headers_str, signature, digest_header)
        """
        request_line = f"{method.upper()} {path} HTTP/1.1"

        if body:
            # 有 body：需要 digest
            digest_bytes = hashlib.sha256(body.encode("utf-8")).digest()
            digest_b64 = base64.b64encode(digest_bytes).decode("utf-8")
            digest_header = f"SHA-256={digest_b64}"

            sig_str = f"x-date: {x_date}\n{request_line}\ndigest: {digest_header}"
            headers_str = "x-date request-line digest"
        else:
            # 沒有 body
            digest_header = None
            sig_str = f"x-date: {x_date}\n{request_line}"
            headers_str = "x-date request-line"

        # HMAC-SHA256 簽名
        signature_bytes = hmac.new(
            self.secret.encode("utf-8"),
            sig_str.encode("utf-8"),
            hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_bytes).decode("utf-8")

        return headers_str, signature, digest_header

    def _build_headers(self, method: str, path: str, body: str = "") -> dict:
        """組裝完整的請求 headers"""
        x_date = self._get_x_date()
        headers_str, signature, digest_header = self._make_signature(
            method, path, x_date, body
        )

        authorization = (
            f'hmac username="{self.username}", '
            f'algorithm="hmac-sha256", '
            f'headers="{headers_str}", '
            f'signature="{signature}"'
        )

        headers = {
            "X-Date": x_date,
            "Authorization": authorization,
            "Content-Type": "application/json",
        }

        if digest_header:
            headers["Digest"] = digest_header

        return headers

    def get_products(self, page: int = 1, limit: int = 25) -> dict:
        """
        取得商品列表

        Args:
            page: 頁數
            limit: 每頁數量（最大 250）

        Returns:
            API 回應 dict
        """
        path = f"/v1/products?page={page}&limit={limit}"
        headers = self._build_headers("GET", path)

        resp = requests.get(
            f"{self.base_url}{path}",
            headers=headers,
            timeout=10
        )

        print(f"Status: {resp.status_code}")
        return resp.json()

    def get_product(self, product_id: str) -> dict:
        """取得單一商品詳細資料"""
        path = f"/v1/products/{product_id}"
        headers = self._build_headers("GET", path)

        resp = requests.get(
            f"{self.base_url}{path}",
            headers=headers,
            timeout=10
        )

        return resp.json()


if __name__ == "__main__":
    # 測試連線
    client = CyberbizClient(
        username="apidemo",
        secret="apidemo",
    )

    print("=== 測試取得商品列表 ===")
    result = client.get_products(page=1, limit=5)
    print(result)