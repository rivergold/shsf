"""Read-only personal Zotero library access; credentials stay in process memory."""
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener


class ZoteroError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ZoteroClient:
    def __init__(self, key, opener=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", key):
            raise ZoteroError("请输入有效的 Zotero API Key。")
        self._key = key
        self.opener = opener or build_opener(NoRedirect())
        self.lock = threading.Lock()
        self.next_request = 0
        self.user_id = None
        self.username = None

    def get(self, path, params=None):
        with self.lock:
            wait = self.next_request - time.monotonic()
            if wait > 0:
                raise ZoteroError(f"Zotero 要求稍后重试，请等待 {int(wait) + 1} 秒。")
            url = "https://api.zotero.org" + path
            if params:
                url += "?" + urlencode(params)
            request = Request(url, headers={"Zotero-API-Key": self._key, "Zotero-API-Version": "3", "Accept": "application/json"})
            try:
                with self.opener.open(request, timeout=25) as response:
                    body = response.read(8_000_001)
                    self._backoff(response.headers)
                    if len(body) > 8_000_000:
                        raise ZoteroError("Zotero 响应过大，请缩小收藏夹范围。")
                    return json.loads(body), int(response.headers.get("Total-Results", "0"))
            except HTTPError as exc:
                self._backoff(exc.headers)
                messages = {401: "API Key 无效。", 403: "API Key 没有个人文献库读取权限。", 404: "收藏夹或条目不存在。", 429: "Zotero 请求过于频繁，请稍后重试。"}
                raise ZoteroError(messages.get(exc.code, "Zotero 暂时无法访问，请稍后重试。")) from None
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, TypeError):
                raise ZoteroError("无法读取 Zotero，请检查网络后重试。") from None

    def _backoff(self, headers):
        for name in ("Backoff", "Retry-After"):
            try:
                delay = float(headers.get(name, 0))
                self.next_request = max(self.next_request, time.monotonic() + min(max(delay, 0), 86400))
            except (ValueError, TypeError):
                pass

    def connect(self, user_id=None):
        info, _ = self.get("/keys/current")
        uid = str(info.get("userID", ""))
        if not uid.isdigit() or not info.get("access", {}).get("user", {}).get("library"):
            raise ZoteroError("API Key 需要开启个人文献库读取权限。")
        if user_id and str(user_id) != uid:
            raise ZoteroError("User ID 与 API Key 所属用户不一致。")
        self.user_id, self.username = uid, info.get("username", "Zotero")
        return self.status()

    def status(self):
        return {"connected": True, "user_id": self.user_id, "username": self.username}

    @staticmethod
    def item_key(key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key):
            raise ZoteroError("无效的 Zotero 条目或收藏夹 ID。")
        return key

    def collections(self, start=0):
        items, total = self.get(f"/users/{self.user_id}/collections", {"start": start, "limit": 100})
        return {"items": [{"key": self.item_key(x["key"]), "name": x["data"]["name"], "parent": x["data"].get("parentCollection")} for x in items], "total": total, "start": start}

    def items(self, collection="", start=0):
        scope = "/collections/" + self.item_key(collection) if collection else ""
        items, total = self.get(f"/users/{self.user_id}{scope}/items/top", {"start": start, "limit": 50, "sort": "dateModified", "direction": "desc"})
        return items, total

    def selected(self, keys):
        if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
            raise ZoteroError("每次请选择 1–100 个条目。")
        keys = list(dict.fromkeys(self.item_key(k) for k in keys))
        items = []
        for offset in range(0, len(keys), 50):
            batch, _ = self.get(f"/users/{self.user_id}/items", {"itemKey": ",".join(keys[offset:offset + 50]), "limit": 100})
            items.extend(batch)
        if {x["key"] for x in items} != set(keys):
            raise ZoteroError("部分条目已删除或不可访问，请刷新预览后重选。")
        return items
