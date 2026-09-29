from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .model_config import MediaModelConfig


class GeminiClient:
    """媒体模型客户端，兼容 Gemini Files API 和 OpenAI 风格模型组。"""

    def __init__(self, config: MediaModelConfig | str, model: str | None = None) -> None:
        if isinstance(config, MediaModelConfig):
            self.config = config
        else:
            if not config:
                raise ValueError("未配置媒体分析 API Key")
            self.config = MediaModelConfig(
                model=model or "gemini-3-flash-preview",
                api_key=config,
                base_url="https://generativelanguage.googleapis.com",
                proxy_url="",
                native_gemini=True,
            )

    async def generate(self, file_path: Path, mime_type: str, prompt: str) -> str:
        if not self.config.native_gemini:
            return await self._generate_openai_compatible(file_path, mime_type, prompt)
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            file_data = await asyncio.to_thread(file_path.read_bytes)
            file_info = await self._upload(session, file_data, mime_type)
            await self._wait_active(session, file_info["name"])
            return await self._generate_content(session, file_info["uri"], mime_type, prompt)

    async def _upload(self, session: aiohttp.ClientSession, data: bytes, mime_type: str) -> dict[str, str]:
        url = f"{self.config.base_url.rstrip('/')}/upload/v1beta/files?key={self.config.api_key}&uploadType=media"
        headers = {"Content-Type": mime_type, "X-Goog-Upload-Protocol": "raw"}
        async with session.post(url, data=data, headers=headers) as response:
            payload = await _json_response(response)
            if response.status != 200:
                raise RuntimeError(f"API 文件上传失败：{json.dumps(payload, ensure_ascii=False)}")
        file_info = payload.get("file")
        if not isinstance(file_info, dict) or not file_info.get("name") or not file_info.get("uri"):
            raise RuntimeError("API 文件上传响应缺少文件标识")
        return {"name": str(file_info["name"]), "uri": str(file_info["uri"])}

    async def _wait_active(self, session: aiohttp.ClientSession, file_name: str) -> None:
        state = "UNKNOWN"
        for _ in range(150):
            async with session.get(
                f"{self.config.base_url.rstrip('/')}/v1beta/{file_name}?key={self.config.api_key}"
            ) as response:
                payload = await _json_response(response)
                if response.status != 200:
                    raise RuntimeError(f"API 文件状态查询失败：{json.dumps(payload, ensure_ascii=False)}")
            state = payload.get("state", "UNKNOWN")
            if state == "ACTIVE":
                return
            if state == "FAILED":
                raise RuntimeError(f"API 云端处理失败：{json.dumps(payload, ensure_ascii=False)}")
            await asyncio.sleep(2)
        raise RuntimeError(f"API 云端处理超时，最终状态：{state}")

    async def _generate_content(self, session: aiohttp.ClientSession, uri: str, mime_type: str, prompt: str) -> str:
        url = (
            f"{self.config.base_url.rstrip('/')}/v1beta/models/{self.config.model}:generateContent"
            f"?key={self.config.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}, {"file_data": {"mime_type": mime_type, "file_uri": uri}}]}],
            "safetySettings": [
                {"category": category, "threshold": "BLOCK_NONE"}
                for category in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        }
        async with session.post(url, json=payload) as response:
            result = await _json_response(response)
            if response.status != 200:
                raise RuntimeError(f"API 响应异常（HTTP {response.status}）：{json.dumps(result, ensure_ascii=False)}")
        try:
            return str(result["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"API 未返回可用内容：{json.dumps(result, ensure_ascii=False)}") from exc

    async def _generate_openai_compatible(self, file_path: Path, mime_type: str, prompt: str) -> str:
        data = await asyncio.to_thread(file_path.read_bytes)
        encoded = base64.b64encode(data).decode("ascii")
        media_content = _openai_media_content(encoded, mime_type)
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, media_content]}],
        }
        for key, value in {"temperature": self.config.temperature, "top_p": self.config.top_p}.items():
            if value is not None:
                payload[key] = value
        if self.config.extra_body:
            payload.update(_parse_extra_body(self.config.extra_body))
        if self.config.top_k is not None:
            payload["top_k"] = self.config.top_k

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        timeout = aiohttp.ClientTimeout(total=300)
        url = _chat_completions_url(self.config.base_url)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            request_kwargs: dict[str, Any] = {"json": payload, "headers": headers}
            if self.config.proxy_url:
                request_kwargs["proxy"] = self.config.proxy_url
            async with session.post(url, **request_kwargs) as response:
                result = await _json_response(response)
                if response.status >= 400:
                    raise RuntimeError(f"模型组请求失败（HTTP {response.status}）：{json.dumps(result, ensure_ascii=False)}")
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            if not content:
                raise ValueError("响应内容为空")
            return str(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"模型组未返回可用内容：{json.dumps(result, ensure_ascii=False)}") from exc


async def _json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        value = await response.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
        text = await response.text()
        raise RuntimeError(f"API 返回非 JSON 内容：{text[:500]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("API 返回结构不是对象")
    return value


def _openai_media_content(encoded: str, mime_type: str) -> dict[str, Any]:
    if mime_type.startswith("audio/"):
        return {"type": "input_audio", "input_audio": {"data": encoded, "format": mime_type.split("/", 1)[1]}}
    return {"type": "video_url", "video_url": {"url": f"data:{mime_type};base64,{encoded}"}}


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/") + "/"
    if normalized.casefold().endswith("chat/completions/"):
        return normalized.rstrip("/")
    if normalized.casefold().endswith("v1/"):
        return urljoin(normalized, "chat/completions")
    return urljoin(normalized, "v1/chat/completions")


def _parse_extra_body(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("模型组 EXTRA_BODY 不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("模型组 EXTRA_BODY 必须是 JSON 对象")
    return parsed
