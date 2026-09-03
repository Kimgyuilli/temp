#!/usr/bin/env python3
"""Archive one calendar month of Slack messages into a Notion database.

This is deliberately dependency-free so it can run on GitHub Actions without a
package installation step. By default it prints a preview. Passing --publish is
required before the script writes anything to Notion.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SLACK_API = "https://slack.com/api"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
KST = ZoneInfo("Asia/Seoul")
MAX_NOTION_BLOCKS_PER_REQUEST = 100
MAX_NOTION_TEXT_LENGTH = 1900
NOTION_SINGLE_PART_LIMIT_BYTES = 20 * 1024 * 1024
NOTION_PART_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_IMAGE_MB = 200
NOTION_TITLE_PROPERTY = "이름"
NOTION_CHANNEL_PROPERTY = "채널"
NOTION_PERIOD_PROPERTY = "기간"
NOTION_STATUS_PROPERTY = "상태"
NOTION_STATUS_IN_PROGRESS = "진행 중"
NOTION_STATUS_COMPLETE = "완료"
NOTION_STATUS_FAILED = "실패"


class ArchiveError(RuntimeError):
    """An expected integration error with a user-actionable message."""


@dataclass(frozen=True)
class MonthWindow:
    label: str
    start: datetime
    end: datetime

    @property
    def oldest(self) -> str:
        return f"{self.start.timestamp():.6f}"

    @property
    def latest(self) -> str:
        return f"{self.end.timestamp():.6f}"

    def contains(self, timestamp: str) -> bool:
        return self.start.timestamp() <= float(timestamp) < self.end.timestamp()


@dataclass(frozen=True)
class DownloadedFile:
    path: str
    filename: str
    content_type: str
    size: int

    def cleanup(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)


def month_window(value: str | None, now: datetime | None = None) -> MonthWindow:
    """Return an exact KST calendar-month window, defaulting to last month."""
    current = now.astimezone(KST) if now else datetime.now(KST)
    if value:
        if not re.fullmatch(r"\d{4}-\d{2}", value):
            raise ArchiveError("월은 YYYY-MM 형식이어야 합니다. 예: 2026-08")
        year, month = map(int, value.split("-"))
        if month < 1 or month > 12:
            raise ArchiveError("월은 01부터 12 사이여야 합니다.")
    else:
        first_this_month = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_day = first_this_month - timedelta(days=1)
        year, month = previous_day.year, previous_day.month

    start = datetime(year, month, 1, tzinfo=KST)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=KST)
    return MonthWindow(f"{year:04d}-{month:02d}", start, end)


def retry_delay(retry_after: str | None, attempt: int) -> float:
    """Honour a numeric Retry-After header, else back off exponentially to 16s."""
    if retry_after:
        with contextlib.suppress(ValueError):
            return float(retry_after)
    return min(2**attempt, 16)


class JsonHttpClient:
    def __init__(self, retries: int = 4, timeout: int = 30):
        self.retries = retries
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"

        return self._send(
            lambda: Request(url, data=payload, headers=request_headers, method=method),
            timeout=self.timeout,
        )

    def _send(
        self,
        build_request: Callable[[], Request],
        *,
        timeout: int,
        error_prefix: str = "",
    ) -> dict[str, Any]:
        """Send one request, retrying 429/5xx and network failures."""
        for attempt in range(self.retries + 1):
            try:
                with urlopen(build_request(), timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < self.retries:
                    time.sleep(retry_delay(error.headers.get("Retry-After"), attempt))
                    continue
                detail = error.read().decode("utf-8", errors="replace")
                raise ArchiveError(
                    f"{error_prefix}HTTP {error.code} 응답: {detail[:500]}"
                ) from error
            except URLError as error:
                if attempt < self.retries:
                    time.sleep(retry_delay(None, attempt))
                    continue
                raise ArchiveError(f"{error_prefix}네트워크 요청 실패: {error.reason}") from error
        raise AssertionError("unreachable")

    def multipart(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes,
        filename: str,
        content_type: str,
        part_number: int | None = None,
    ) -> dict[str, Any]:
        """POST one multipart file body, optionally as a numbered Notion part."""
        boundary = f"----slack-notion-{uuid.uuid4().hex}"
        body = bytearray()
        if part_number is not None:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(b'Content-Disposition: form-data; name="part_number"\r\n\r\n')
            body.extend(f"{part_number}\r\n".encode())
        ascii_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "upload.bin"
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="file"; filename="{ascii_filename}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(content)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request_headers = {
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **headers,
        }

        return self._send(
            lambda: Request(url, data=bytes(body), headers=request_headers, method="POST"),
            timeout=max(self.timeout, 120),
            error_prefix="파일 업로드 ",
        )


class SlackClient:
    def __init__(self, token: str, http: JsonHttpClient | None = None):
        self.token = token
        self.http = http or JsonHttpClient()

    def call(
        self,
        method: str,
        *,
        http_method: str = "GET",
        **params: Any,
    ) -> dict[str, Any]:
        result = self.http.request(
            http_method,
            f"{SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {self.token}"},
            params=params if http_method == "GET" else None,
            body=params if http_method != "GET" else None,
        )
        if not result.get("ok"):
            raise ArchiveError(f"Slack API {method} 실패: {result.get('error', 'unknown_error')}")
        return result

    def auth_test(self) -> dict[str, Any]:
        return self.call("auth.test")

    def download_file(self, file: dict[str, Any], max_bytes: int) -> DownloadedFile:
        url = file.get("url_private_download") or file.get("url_private")
        if not url:
            raise ArchiveError("Slack 파일에 다운로드 URL이 없습니다.")
        expected_size = int(file.get("size") or 0)
        if expected_size and expected_size > max_bytes:
            raise ArchiveError(f"설정된 이미지 한도({max_bytes // 1024 // 1024}MB)를 초과합니다.")

        filename = safe_filename(file.get("name") or file.get("title") or f"slack-image-{file.get('id', 'unknown')}")
        content_type = safe_content_type(file.get("mimetype"))
        for attempt in range(self.http.retries + 1):
            temp = tempfile.NamedTemporaryFile(prefix="slack-image-", delete=False)
            temp_path = temp.name
            try:
                request = Request(url, headers={"Authorization": f"Bearer {self.token}"})
                with urlopen(request, timeout=120) as response, temp:
                    response_type = safe_content_type(response.headers.get_content_type())
                    if response_type.startswith("image/"):
                        content_type = response_type
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ArchiveError(
                                f"다운로드 중 설정된 이미지 한도({max_bytes // 1024 // 1024}MB)를 초과했습니다."
                            )
                        temp.write(chunk)
                return DownloadedFile(temp_path, filename, content_type, total)
            except (HTTPError, URLError) as error:
                with contextlib.suppress(Exception):
                    temp.close()
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_path)
                retryable = (
                    not isinstance(error, HTTPError)
                    or error.code == 429
                    or 500 <= error.code < 600
                )
                if retryable and attempt < self.http.retries:
                    retry_after = error.headers.get("Retry-After") if isinstance(error, HTTPError) else None
                    time.sleep(retry_delay(retry_after, attempt))
                    continue
                if isinstance(error, HTTPError):
                    raise ArchiveError(f"Slack 이미지 다운로드 실패: HTTP {error.code}") from error
                raise ArchiveError(f"Slack 이미지 다운로드 실패: {error.reason}") from error
            except Exception:
                with contextlib.suppress(Exception):
                    temp.close()
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_path)
                raise
        raise AssertionError("unreachable")

    def users(self) -> dict[str, str]:
        cursor = ""
        names: dict[str, str] = {}
        while True:
            result = self.call("users.list", limit=200, cursor=cursor)
            for user in result.get("members", []):
                profile = user.get("profile", {})
                names[user["id"]] = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user.get("real_name")
                    or user.get("name")
                    or user["id"]
                )
            cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return names

    def member_channels(
        self,
        *,
        auto_join_public: bool = False,
    ) -> list[dict[str, Any]]:
        cursor = ""
        channels: list[dict[str, Any]] = []
        while True:
            result = self.call(
                "conversations.list",
                types="public_channel,private_channel",
                exclude_archived="true",
                limit=200,
                cursor=cursor,
            )
            for channel in result.get("channels", []):
                if not channel.get("is_member"):
                    if not auto_join_public or channel.get("is_private"):
                        continue
                    joined = self.call(
                        "conversations.join",
                        http_method="POST",
                        channel=channel["id"],
                    )
                    channel = joined.get("channel") or {**channel, "is_member": True}
                channels.append(channel)
            cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return sorted(channels, key=lambda item: item.get("name", item["id"]))

    def channel_messages(self, channel_id: str, window: MonthWindow) -> list[dict[str, Any]]:
        cursor = ""
        roots: list[dict[str, Any]] = []
        while True:
            result = self.call(
                "conversations.history",
                channel=channel_id,
                oldest=window.oldest,
                latest=window.latest,
                inclusive="true",
                limit=200,
                cursor=cursor,
            )
            roots.extend(
                message
                for message in result.get("messages", [])
                if window.contains(message["ts"])
            )
            cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break

        output: list[dict[str, Any]] = []
        for root in sorted(roots, key=lambda item: float(item["ts"])):
            root = dict(root)
            root["_replies"] = []
            if root.get("reply_count", 0):
                root["_replies"] = self.thread_replies(channel_id, root["ts"], window)
            output.append(root)
        return output

    def thread_replies(
        self, channel_id: str, root_ts: str, window: MonthWindow
    ) -> list[dict[str, Any]]:
        cursor = ""
        replies: list[dict[str, Any]] = []
        while True:
            result = self.call(
                "conversations.replies",
                channel=channel_id,
                ts=root_ts,
                oldest=window.oldest,
                latest=window.latest,
                inclusive="true",
                limit=200,
                cursor=cursor,
            )
            for message in result.get("messages", []):
                if message["ts"] != root_ts and window.contains(message["ts"]):
                    replies.append(message)
            cursor = result.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return sorted(replies, key=lambda item: float(item["ts"]))


class NotionClient:
    def __init__(self, token: str, data_source_id: str, http: JsonHttpClient | None = None):
        self.data_source_id = data_source_id
        self.http = http or JsonHttpClient()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        }

    def validate_schema(self) -> dict[str, Any]:
        result = self.http.request(
            "GET",
            f"{NOTION_API}/data_sources/{self.data_source_id}",
            headers=self.headers,
        )
        properties = result.get("properties", {})
        expected = {
            NOTION_TITLE_PROPERTY: "title",
            NOTION_CHANNEL_PROPERTY: "select",
            NOTION_PERIOD_PROPERTY: "select",
            NOTION_STATUS_PROPERTY: "status",
        }
        errors = [
            f"{name}({property_type})"
            for name, property_type in expected.items()
            if properties.get(name, {}).get("type") != property_type
        ]
        if errors:
            raise ArchiveError(
                "Notion DB에 다음 속성이 필요합니다: " + ", ".join(errors)
            )

        status_options = {
            option.get("name")
            for option in properties[NOTION_STATUS_PROPERTY]
            .get("status", {})
            .get("options", [])
        }
        required_statuses = {
            NOTION_STATUS_IN_PROGRESS,
            NOTION_STATUS_COMPLETE,
            NOTION_STATUS_FAILED,
        }
        missing_statuses = sorted(required_statuses - status_options)
        if missing_statuses:
            raise ArchiveError(
                f"Notion DB의 {NOTION_STATUS_PROPERTY} 속성에 다음 옵션을 추가하세요: "
                + ", ".join(missing_statuses)
            )
        return properties

    def ensure_select_options(
        self,
        properties: dict[str, Any],
        channel_labels: set[str],
        period: str,
    ) -> dict[str, list[str]]:
        """Create missing channel/period labels without removing existing options."""
        requested = {
            NOTION_CHANNEL_PROPERTY: channel_labels,
            NOTION_PERIOD_PROPERTY: {period},
        }
        updates: dict[str, Any] = {}
        added: dict[str, list[str]] = {}
        for property_name, values in requested.items():
            existing = (
                properties[property_name]
                .get("select", {})
                .get("options", [])
            )
            existing_names = {
                option.get("name") for option in existing if option.get("name")
            }
            missing = sorted(values - existing_names)
            if not missing:
                continue
            preserved = [
                {"id": option["id"]}
                if option.get("id")
                else {"name": option["name"]}
                for option in existing
                if option.get("id") or option.get("name")
            ]
            updates[property_name] = {
                "select": {
                    "options": preserved + [{"name": value} for value in missing]
                }
            }
            added[property_name] = missing

        if updates:
            self.http.request(
                "PATCH",
                f"{NOTION_API}/data_sources/{self.data_source_id}",
                headers=self.headers,
                body={"properties": updates},
            )
        return added

    def exact_entry(self, channel_label: str, period: str) -> dict[str, Any] | None:
        result = self.http.request(
            "POST",
            f"{NOTION_API}/data_sources/{self.data_source_id}/query",
            headers=self.headers,
            body={
                "filter": {
                    "and": [
                        {
                            "property": NOTION_CHANNEL_PROPERTY,
                            "select": {"equals": channel_label},
                        },
                        {
                            "property": NOTION_PERIOD_PROPERTY,
                            "select": {"equals": period},
                        },
                    ]
                },
                "page_size": 1,
            },
        )
        return next(
            (
                page
                for page in result.get("results", [])
                if not page.get("in_trash", False)
            ),
            None,
        )

    def upload_file(self, downloaded: DownloadedFile) -> str:
        """Upload a local file to Notion, using multi-part mode above 20 MiB."""
        multi_part = downloaded.size > NOTION_SINGLE_PART_LIMIT_BYTES
        number_of_parts = (
            max(1, math.ceil(downloaded.size / NOTION_PART_BYTES)) if multi_part else 1
        )
        create_body: dict[str, Any] = {
            "mode": "multi_part" if multi_part else "single_part",
            "filename": downloaded.filename,
            "content_type": downloaded.content_type,
        }
        if multi_part:
            create_body["number_of_parts"] = number_of_parts
        upload = self.http.request(
            "POST",
            f"{NOTION_API}/file_uploads",
            headers=self.headers,
            body=create_body,
        )
        upload_id = upload.get("id")
        if not upload_id:
            raise ArchiveError("Notion이 파일 업로드 ID를 반환하지 않았습니다.")
        upload_url = upload.get("upload_url") or f"{NOTION_API}/file_uploads/{upload_id}/send"

        with open(downloaded.path, "rb") as source:
            for part_number in range(1, number_of_parts + 1):
                content = source.read(NOTION_PART_BYTES if multi_part else downloaded.size + 1)
                if not content and downloaded.size:
                    raise ArchiveError("Notion 업로드 전 이미지 파일을 끝까지 읽지 못했습니다.")
                part_result = self.http.multipart(
                    upload_url,
                    headers=self.headers,
                    content=content,
                    filename=downloaded.filename,
                    content_type=downloaded.content_type,
                    part_number=part_number if multi_part else None,
                )
                if not multi_part:
                    upload = part_result
                time.sleep(0.35)

        if multi_part:
            upload = self.http.request(
                "POST",
                upload.get("complete_url") or f"{NOTION_API}/file_uploads/{upload_id}/complete",
                headers=self.headers,
            )
        if upload.get("status") != "uploaded":
            raise ArchiveError(f"Notion 이미지 업로드 상태가 예상과 다릅니다: {upload.get('status')}")
        return upload_id

    def update_status(self, page_id: str, status: str) -> None:
        self.http.request(
            "PATCH",
            f"{NOTION_API}/pages/{page_id}",
            headers=self.headers,
            body={
                "properties": {
                    NOTION_STATUS_PROPERTY: {"status": {"name": status}}
                }
            },
        )

    def create_archive_entry(
        self,
        title: str,
        channel_label: str,
        period: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        page = self.http.request(
            "POST",
            f"{NOTION_API}/pages",
            headers=self.headers,
            body={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": self.data_source_id,
                },
                "properties": {
                    NOTION_TITLE_PROPERTY: {
                        "title": [{"type": "text", "text": {"content": title}}],
                    },
                    NOTION_CHANNEL_PROPERTY: {"select": {"name": channel_label}},
                    NOTION_PERIOD_PROPERTY: {"select": {"name": period}},
                    NOTION_STATUS_PROPERTY: {
                        "status": {"name": NOTION_STATUS_IN_PROGRESS}
                    },
                },
            },
        )
        page_id = page.get("id")
        if not page_id:
            raise ArchiveError("Notion이 생성된 DB 페이지 ID를 반환하지 않았습니다.")
        try:
            for batch in chunked(blocks, MAX_NOTION_BLOCKS_PER_REQUEST):
                self.http.request(
                    "PATCH",
                    f"{NOTION_API}/blocks/{page_id}/children",
                    headers=self.headers,
                    body={"children": batch},
                )
                time.sleep(0.35)  # Keep comfortably below Notion's average 3 req/s limit.
            self.update_status(page_id, NOTION_STATUS_COMPLETE)
        except Exception:
            with contextlib.suppress(Exception):
                self.update_status(page_id, NOTION_STATUS_FAILED)
            raise
        return {"status": "created", "url": page.get("url"), "id": page_id}


def channel_label(channel: dict[str, Any]) -> str:
    return f"#{channel.get('name', channel['id'])}"


def entry_title(window: MonthWindow, label: str) -> str:
    return f"Slack · {window.label} · {label}"


def safe_filename(value: str) -> str:
    """Keep the name useful while avoiding paths and Notion's 900-byte limit."""
    name = os.path.basename(value.replace("\\", "/")).strip() or "slack-image"
    encoded = name.encode("utf-8")
    if len(encoded) <= 800:
        return name
    root, extension = os.path.splitext(name)
    allowed = max(1, 780 - len(extension.encode("utf-8")))
    shortened = root.encode("utf-8")[:allowed].decode("utf-8", errors="ignore")
    return shortened + extension


def safe_content_type(value: Any) -> str:
    content_type = str(value or "application/octet-stream").lower()
    if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type):
        return content_type
    return "application/octet-stream"


def is_image_file(file: dict[str, Any]) -> bool:
    return str(file.get("mimetype") or "").lower().startswith("image/")


def all_messages(messages: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for message in messages:
        yield message
        yield from message.get("_replies", [])


def upload_message_images(
    messages: list[dict[str, Any]],
    slack: SlackClient,
    notion: NotionClient,
    max_bytes: int,
) -> tuple[int, int]:
    """Add private Notion upload IDs to Slack image objects in-place."""
    uploaded = failed = 0
    for message in all_messages(messages):
        for file in message.get("files", []):
            if not is_image_file(file):
                continue
            downloaded: DownloadedFile | None = None
            try:
                downloaded = slack.download_file(file, max_bytes)
                file["_notion_upload_id"] = notion.upload_file(downloaded)
                uploaded += 1
            except ArchiveError as error:
                file["_archive_error"] = str(error)
                failed += 1
                filename = file.get("name") or file.get("title") or file.get("id") or "이미지"
                print(f"경고: {filename} 원본을 업로드하지 못했습니다: {error}", file=sys.stderr)
            finally:
                if downloaded:
                    downloaded.cleanup()
    return uploaded, failed


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def slack_text(text: str, users: dict[str, str]) -> str:
    """Make the most common Slack mrkdwn tokens readable outside Slack."""
    text = re.sub(r"<@([A-Z0-9]+)>", lambda match: f"@{users.get(match.group(1), match.group(1))}", text)
    text = re.sub(r"<([^>|]+)\|([^>]+)>", lambda match: f"{match.group(2)} ({match.group(1)})", text)
    text = re.sub(r"<((?:https?|mailto):[^>]+)>", r"\1", text)
    return text


def message_permalink(workspace_url: str, channel_id: str, timestamp: str) -> str | None:
    if not workspace_url:
        return None
    return f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{timestamp.replace('.', '')}"


def message_author(message: dict[str, Any], users: dict[str, str]) -> str:
    user_id = message.get("user")
    if user_id:
        return users.get(user_id, user_id)
    return message.get("username") or message.get("bot_profile", {}).get("name") or "Slack Bot"


def message_details(message: dict[str, Any]) -> str:
    extras: list[str] = []
    reactions = message.get("reactions", [])
    if reactions:
        extras.append(" ".join(f":{item['name']}:×{item.get('count', 0)}" for item in reactions))
    files = message.get("files", [])
    if files:
        extras.append("파일: " + ", ".join(item.get("title") or item.get("name") or "첨부파일" for item in files))
    return " · ".join(extras)


def kst_datetime(timestamp: str) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(KST)


def rich_text(content: str, *, bold: bool = False, link: str | None = None) -> list[dict[str, Any]]:
    if not content:
        return []
    pieces: list[dict[str, Any]] = []
    for index in range(0, len(content), MAX_NOTION_TEXT_LENGTH):
        text: dict[str, Any] = {"content": content[index : index + MAX_NOTION_TEXT_LENGTH]}
        if link:
            text["link"] = {"url": link}
        pieces.append(
            {
                "type": "text",
                "text": text,
                "annotations": {"bold": bold},
            }
        )
    return pieces


def paragraph_block(
    message: dict[str, Any],
    users: dict[str, str],
    channel_id: str,
    workspace_url: str,
    *,
    reply: bool = False,
) -> dict[str, Any]:
    sent_at = kst_datetime(message["ts"])
    prefix = "↳ " if reply else ""
    display_time = f"{sent_at:%m-%d %H:%M}" if reply else f"{sent_at:%H:%M}"
    header = f"{prefix}{display_time} {message_author(message, users)}  "
    body = slack_text(message.get("text") or "(본문 없음)", users)
    details = message_details(message)
    if details:
        body += f"\n{details}"
    content = rich_text(header, bold=True) + rich_text(body)
    permalink = message_permalink(workspace_url, channel_id, message["ts"])
    if permalink:
        content += rich_text("  원문", link=permalink)
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": content}}


def image_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for file in message.get("files", []):
        upload_id = file.get("_notion_upload_id")
        if not upload_id:
            continue
        filename = file.get("title") or file.get("name") or "Slack 이미지"
        blocks.append(
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "file_upload",
                    "file_upload": {"id": upload_id},
                    "caption": rich_text(filename),
                },
            }
        )
    return blocks


def archive_blocks(
    channel: dict[str, Any],
    messages: list[dict[str, Any]],
    users: dict[str, str],
    window: MonthWindow,
    workspace_url: str,
) -> list[dict[str, Any]]:
    message_count = sum(1 + len(message.get("_replies", [])) for message in messages)
    blocks: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📦"},
                "rich_text": rich_text(
                    f"기간: {window.label}-01 ~ {window.label}-{calendar.monthrange(window.start.year, window.start.month)[1]:02d}"
                    f" · 채널 ID: {channel['id']} · 메시지/답글: {message_count}개"
                ),
            },
        }
    ]
    current_date = None
    for message in messages:
        date = kst_datetime(message["ts"]).date()
        if date != current_date:
            current_date = date
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": rich_text(f"{date:%Y-%m-%d}")},
                }
            )
        blocks.append(paragraph_block(message, users, channel["id"], workspace_url))
        blocks.extend(image_blocks(message))
        for reply_message in message.get("_replies", []):
            blocks.append(
                paragraph_block(reply_message, users, channel["id"], workspace_url, reply=True)
            )
            blocks.extend(image_blocks(reply_message))
    if not messages:
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": rich_text("이 기간에 메시지가 없습니다.")},
            }
        )
    return blocks


def markdown_preview(
    channel: dict[str, Any],
    messages: list[dict[str, Any]],
    users: dict[str, str],
    window: MonthWindow,
    workspace_url: str,
) -> str:
    label = channel_label(channel)
    title = entry_title(window, label)
    lines = [
        f"[Notion DB] 이름={title} · 채널={label} · 기간={window.label} · 상태={NOTION_STATUS_COMPLETE}",
        "",
        f"# {title}",
        "",
    ]
    current_date = None
    for message in messages:
        sent_at = kst_datetime(message["ts"])
        if sent_at.date() != current_date:
            current_date = sent_at.date()
            lines.extend([f"## {current_date:%Y-%m-%d}", ""])
        for item, prefix in [(message, ""), *[(reply, "  ↳ ") for reply in message.get("_replies", [])]]:
            item_time = kst_datetime(item["ts"])
            display_time = f"{item_time:%m-%d %H:%M}" if prefix else f"{item_time:%H:%M}"
            text = slack_text(item.get("text") or "(본문 없음)", users).replace("\n", " ")
            line = f"- {prefix}**{display_time} {message_author(item, users)}** — {text}"
            details = message_details(item)
            if details:
                line += f" · {details}"
            permalink = message_permalink(workspace_url, channel["id"], item["ts"])
            if permalink:
                line += f" · [원문]({permalink})"
            lines.append(line)
        lines.append("")
    if not messages:
        lines.append("이 기간에 메시지가 없습니다.")
    return "\n".join(lines).rstrip() + "\n"


def mock_archive(window: MonthWindow) -> tuple[list[dict[str, Any]], dict[str, str]]:
    def ts(day: int, hour: int, minute: int) -> str:
        return f"{datetime(window.start.year, window.start.month, day, hour, minute, tzinfo=KST).timestamp():.6f}"

    channels = [
        {
            "id": "C01PRODUCT",
            "name": "product",
            "is_private": False,
            "messages": [
                {
                    "ts": ts(3, 9, 14),
                    "user": "U01MIN",
                    "text": "이번 배포는 목요일 오전으로 변경하겠습니다. <@U02SEO> QA 일정 확인 부탁드려요.",
                    "reactions": [{"name": "white_check_mark", "count": 3}],
                    "_replies": [
                        {
                            "ts": ts(3, 9, 20),
                            "user": "U02SEO",
                            "text": "확인했습니다. QA 일정도 하루 미루겠습니다.",
                        },
                        {
                            "ts": ts(3, 9, 31),
                            "user": "U01MIN",
                            "text": "감사합니다! 변경된 일정은 <https://example.com/release|릴리스 문서>에 반영했어요.",
                        },
                    ],
                },
                {
                    "ts": ts(18, 14, 5),
                    "user": "U03KIM",
                    "text": "8월 고객 인터뷰 결과 화면과 메모를 공유합니다.",
                    "files": [
                        {
                            "id": "F01IMAGE",
                            "name": "interview-result.png",
                            "title": "고객 인터뷰 결과",
                            "mimetype": "image/png",
                            "size": 184320,
                            "url_private_download": "https://files.slack.com/demo/interview-result.png",
                        },
                        {
                            "id": "F02PDF",
                            "name": "interview-notes.pdf",
                            "title": "고객 인터뷰 메모",
                            "mimetype": "application/pdf",
                        },
                    ],
                    "_replies": [],
                },
            ],
        }
    ]
    return channels, {"U01MIN": "민수", "U02SEO": "서연", "U03KIM": "김PM"}


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ArchiveError(f"{name}은 true 또는 false여야 합니다.")


def run(args: argparse.Namespace) -> int:
    if args.mock and args.publish:
        raise ArchiveError("--mock과 --publish는 함께 사용할 수 없습니다.")
    window = month_window(args.month or os.getenv("ARCHIVE_MONTH"))
    workspace_url = os.getenv("SLACK_WORKSPACE_URL", "")
    slack: SlackClient | None = None

    if args.mock:
        channels, users = mock_archive(window)
        workspace_url = workspace_url or "https://demo-workspace.slack.com"
    else:
        slack_token = os.getenv("SLACK_BOT_TOKEN")
        if not slack_token:
            raise ArchiveError("SLACK_BOT_TOKEN 환경 변수가 필요합니다. 데모만 보려면 --mock을 사용하세요.")
        slack = SlackClient(slack_token)
        identity = slack.auth_test()
        if not workspace_url and identity.get("url"):
            workspace_url = identity["url"]
        users = slack.users()
        auto_join_public = environment_flag("AUTO_JOIN_PUBLIC_CHANNELS")
        channels = []
        for channel in slack.member_channels(auto_join_public=auto_join_public):
            channel = dict(channel)
            channel["messages"] = slack.channel_messages(channel["id"], window)
            channels.append(channel)

    if not args.publish:
        previews = [
            markdown_preview(channel, channel["messages"], users, window, workspace_url)
            for channel in channels
        ]
        print("\n---\n\n".join(previews))
        print(f"\n[미리보기] {len(channels)}개 채널. Notion에는 쓰지 않았습니다.", file=sys.stderr)
        return 0

    notion_token = os.getenv("NOTION_TOKEN")
    notion_data_source = os.getenv("NOTION_DATA_SOURCE_ID")
    if not notion_token or not notion_data_source:
        raise ArchiveError("게시하려면 NOTION_TOKEN과 NOTION_DATA_SOURCE_ID가 필요합니다.")

    notion = NotionClient(notion_token, notion_data_source)
    properties = notion.validate_schema()
    if channels:
        added_labels = notion.ensure_select_options(
            properties,
            {channel_label(channel) for channel in channels},
            window.label,
        )
        for property_name, values in added_labels.items():
            print(f"labels   {property_name}: {', '.join(values)}")
    try:
        configured_max = (
            args.max_image_mb
            if args.max_image_mb is not None
            else os.getenv("MAX_IMAGE_MB") or DEFAULT_MAX_IMAGE_MB
        )
        max_image_mb = int(configured_max)
    except ValueError as error:
        raise ArchiveError("MAX_IMAGE_MB는 양의 정수여야 합니다.") from error
    if max_image_mb <= 0 or max_image_mb > 5120:
        raise ArchiveError("MAX_IMAGE_MB는 1부터 5120 사이여야 합니다.")
    max_image_bytes = max_image_mb * 1024 * 1024
    created = skipped = 0
    for channel in channels:
        label = channel_label(channel)
        title = entry_title(window, label)
        existing = notion.exact_entry(label, window.label)
        if existing:
            skipped += 1
            print(f"skipped  {title}  {existing.get('url') or existing['id']}")
            continue
        assert slack is not None
        uploaded, failed = upload_message_images(
            channel["messages"], slack, notion, max_image_bytes
        )
        if uploaded or failed:
            print(f"images   {title}  업로드 {uploaded}개, 실패 {failed}개")
        blocks = archive_blocks(
            channel, channel["messages"], users, window, workspace_url
        )
        result = notion.create_archive_entry(title, label, window.label, blocks)
        created += 1
        print(f"created  {title}  {result.get('url') or result['id']}")
    print(f"완료: 생성 {created}개, 기존 페이지 건너뜀 {skipped}개")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="아카이빙할 KST 기준 월(YYYY-MM). 기본값은 지난달")
    parser.add_argument("--mock", action="store_true", help="Slack 없이 샘플 데이터로 미리보기")
    parser.add_argument("--publish", action="store_true", help="Notion에 실제 페이지 생성")
    parser.add_argument(
        "--max-image-mb",
        type=int,
        help=f"이미지 하나의 최대 다운로드 크기(MB). 기본값 {DEFAULT_MAX_IMAGE_MB}",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except ArchiveError as error:
        print(f"오류: {error}", file=sys.stderr)
        raise SystemExit(2)
