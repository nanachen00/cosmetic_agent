import os
from threading import Lock
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

if TYPE_CHECKING:
    from zep_cloud.client import Zep
else:
    Zep = Any

load_dotenv()

# Secrets must come from the runtime environment; never keep a source fallback.
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "").strip()
ZEP_TIMEOUT_SECONDS = float(os.getenv("ZEP_TIMEOUT_SECONDS", "3"))

_zep_client = None
_zep_client_lock = Lock()


def get_zep_client() -> Zep:
    """Return one process-wide client so HTTP connections are reused."""
    global _zep_client
    if not ZEP_API_KEY:
        raise ValueError("❌ 未設定 ZEP_API_KEY，請確認 .env 檔案或環境變數。")

    if _zep_client is None:
        with _zep_client_lock:
            if _zep_client is None:
                try:
                    from zep_cloud.client import Zep as ZepClient
                except ModuleNotFoundError as exc:
                    raise RuntimeError(
                        "Zep memory requires the optional zep-cloud package"
                    ) from exc
                _zep_client = ZepClient(
                    api_key=ZEP_API_KEY,
                    timeout=ZEP_TIMEOUT_SECONDS,
                    follow_redirects=True,
                )
    return _zep_client


if __name__ == "__main__":
    try:
        get_zep_client()
        print("🚀 [Zep Client] 成功建立共用連線！")
    except Exception as error:
        print(f"❌ [Zep Client] 連線失敗: {type(error).__name__}")
