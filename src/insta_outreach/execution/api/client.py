"""Minimal Graph API client for the Instagram Platform (Tier 1).

Endpoints and error codes follow the research in docs/RESEARCH.md (Graph API
v26.0, Sept 2026). The access token is sent as a Bearer header, never in the
URL, so it cannot leak into proxy or access logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from insta_outreach.config import ApiSettings
from insta_outreach.domain.enums import ExecutionStatus

BUSINESS_DISCOVERY_FIELDS = (
    "id,username,name,biography,website,followers_count,follows_count,media_count,"
    "profile_picture_url,media.limit({limit}){{caption,timestamp,media_type,media_product_type,"
    "permalink,like_count,comments_count}}"
)
USER_PROFILE_FIELDS = (
    "name,username,profile_pic,follower_count,is_user_follow_business,is_business_follow_user,is_verified_user"
)
MESSAGE_FIELDS = "messages{id,created_time,from,to,message}"


@dataclass(eq=False)
class GraphApiError(Exception):
    http_status: int
    code: int | None
    subcode: int | None
    message: str
    error_type: str | None = None
    fbtrace_id: str | None = None
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        return f"Graph API error {self.code}/{self.subcode} (HTTP {self.http_status}): {self.message}"


class GraphTransportError(Exception):
    """Network-level failure: the outcome of the request is unknown."""


# (code, subcode or None) -> (status, machine code)
_ERROR_MAP: dict[tuple[int, int | None], tuple[ExecutionStatus, str]] = {
    (10, 2534022): (ExecutionStatus.NOT_PERMITTED, "outside_messaging_window"),
    (10, 2018278): (ExecutionStatus.NOT_PERMITTED, "outside_messaging_window"),
    (551, 1545041): (ExecutionStatus.NOT_PERMITTED, "recipient_unavailable"),
    (551, None): (ExecutionStatus.NOT_PERMITTED, "recipient_not_receiving"),
    (100, 2534014): (ExecutionStatus.TARGET_NOT_FOUND, "no_matching_user"),
    (100, 2534025): (ExecutionStatus.NOT_PERMITTED, "comment_invalid_for_private_reply"),
    (100, 2534029): (ExecutionStatus.ACCOUNT_RESTRICTED, "business_blocked_from_messaging"),
    (110, 2207013): (ExecutionStatus.TARGET_NOT_FOUND, "not_found_or_not_professional"),
    (200, 2534041): (ExecutionStatus.HUMAN_ACTION_REQUIRED, "dm_api_access_disabled_in_app_settings"),
    (9000001, None): (ExecutionStatus.PERMANENT_FAILURE, "message_no_longer_available"),
}
_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80002})


def map_error(error: GraphApiError) -> tuple[ExecutionStatus, str]:
    if error.code is not None:
        exact = _ERROR_MAP.get((error.code, error.subcode))
        if exact:
            return exact
        if error.code in _RATE_LIMIT_CODES:
            return ExecutionStatus.RATE_LIMITED, f"rate_limit_{error.code}"
        if error.code == 190:
            return ExecutionStatus.LOGIN_REQUIRED, f"token_invalid_{error.subcode or 'unknown'}"
        generic = _ERROR_MAP.get((error.code, None))
        if generic:
            return generic
        if error.code == 10 or 200 <= error.code <= 299:
            return ExecutionStatus.HUMAN_ACTION_REQUIRED, f"permission_{error.code}"
        if error.code in (1, 2):
            return ExecutionStatus.RETRYABLE_FAILURE, "service_temporarily_unavailable"
    if error.http_status == 429:
        return ExecutionStatus.RATE_LIMITED, "http_429"
    if error.http_status >= 500:
        return ExecutionStatus.RETRYABLE_FAILURE, f"http_{error.http_status}"
    return ExecutionStatus.PERMANENT_FAILURE, f"graph_{error.code}_{error.subcode}"


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header and header.isdigit():
        return float(header)
    usage = response.headers.get("x-business-use-case-usage")
    if usage:
        try:
            parsed = json.loads(usage)
        except ValueError:
            return None
        minutes = [
            entry.get("estimated_time_to_regain_access", 0)
            for entries in parsed.values()
            for entry in (entries if isinstance(entries, list) else [])
            if isinstance(entry, dict)
        ]
        if minutes and max(minutes) > 0:
            return float(max(minutes)) * 60
    return None


class GraphApiClient:
    def __init__(self, settings: ApiSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.access_token or not settings.ig_user_id:
            raise ValueError("API adapter requires api.access_token and api.ig_user_id")
        self._settings = settings
        self._ig_user_id = settings.ig_user_id
        self._http = httpx.AsyncClient(
            base_url=settings.resolved_base_url,
            timeout=settings.timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {settings.access_token.get_secret_value()}"},
        )

    @property
    def ig_user_id(self) -> str:
        return self._ig_user_id

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise GraphTransportError(f"{type(exc).__name__}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_error or (isinstance(payload, dict) and "error" in payload):
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise GraphApiError(
                http_status=response.status_code,
                code=err.get("code"),
                subcode=err.get("error_subcode"),
                message=str(err.get("message") or response.text[:300]),
                error_type=err.get("type"),
                fbtrace_id=err.get("fbtrace_id"),
                retry_after_seconds=_retry_after(response),
            )
        return payload if isinstance(payload, dict) else {"data": payload}

    # -- messaging ------------------------------------------------------------
    async def send_text(self, recipient_igsid: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/{self._ig_user_id}/messages",
            body={"recipient": {"id": recipient_igsid}, "message": {"text": text}},
        )

    async def send_private_reply(self, comment_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/{self._ig_user_id}/messages",
            body={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )

    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/{comment_id}/replies", body={"message": text})

    # -- conversations ------------------------------------------------------------
    async def list_conversations(self, *, user_igsid: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"platform": "instagram", "limit": limit, "fields": "id,updated_time,participants"}
        if user_igsid:
            params["user_id"] = user_igsid
        payload = await self._request("GET", f"/{self._ig_user_id}/conversations", params=params)
        return list(payload.get("data", []))

    async def get_conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/{conversation_id}", params={"fields": MESSAGE_FIELDS})
        return list((payload.get("messages") or {}).get("data", []))

    async def get_user_profile(self, igsid: str) -> dict[str, Any]:
        return await self._request("GET", f"/{igsid}", params={"fields": USER_PROFILE_FIELDS})

    # -- discovery (Facebook Login only) -------------------------------------------
    async def business_discovery(self, username: str, media_limit: int = 12) -> dict[str, Any]:
        fields = BUSINESS_DISCOVERY_FIELDS.format(limit=media_limit)
        payload = await self._request(
            "GET",
            f"/{self._ig_user_id}",
            params={"fields": f"business_discovery.username({username}){{{fields}}}"},
        )
        return dict(payload.get("business_discovery") or {})

    async def close(self) -> None:
        await self._http.aclose()
