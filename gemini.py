from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp


class GeminiClient:
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("未配置 GEMINI_API_KEY")
        self.api_key = api_key
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com"

    async def generate(self, file_path: Path, mime_type: str, prompt: str) -> str:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            file_data = await asyncio.to_thread(file_path.read_bytes)
            file_info = await self._upload(session, file_data, mime_type)
            await self._wait_active(session, file_info["name"])
            return await self._generate_content(session, file_info["uri"], mime_type, prompt)

    async def _upload(self, session: aiohttp.ClientSession, data: bytes, mime_type: str) -> dict[str, str]:
        url = f"{self.base_url}/upload/v1beta/files?key={self.api_key}&uploadType=media"
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
            async with session.get(f"{self.base_url}/v1beta/{file_name}?key={self.api_key}") as response:
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
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent?key={self.api_key}"
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


async def _json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        value = await response.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
        text = await response.text()
        raise RuntimeError(f"API 返回非 JSON 内容：{text[:500]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("API 返回结构不是对象")
    return value
