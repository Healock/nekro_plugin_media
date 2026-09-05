from __future__ import annotations

from pydantic import BaseModel, Field


class BilibiliViewData(BaseModel):
    title: str = "未知标题"
    uploader: str = "未知 UP 主"
    desc: str = "暂无简介"
    duration: int = 0
    aid: int | None = None

    @classmethod
    def from_api_payload(cls, payload: object) -> "BilibiliViewData":
        if not isinstance(payload, dict):
            return cls()
        owner = payload.get("owner")
        owner_name = owner.get("name") if isinstance(owner, dict) else None
        try:
            aid = int(payload["aid"]) if payload.get("aid") is not None else None
            duration = int(payload.get("duration") or 0)
        except (TypeError, ValueError):
            return cls()
        return cls(
            title=str(payload.get("title") or "未知标题"),
            uploader=str(owner_name or "未知 UP 主"),
            desc=str(payload.get("desc") or "暂无简介"),
            duration=duration,
            aid=aid,
        )


class BilibiliComment(BaseModel):
    message: str = ""
    like: int = 0


class BilibiliContext(BaseModel):
    title: str = "未知标题"
    uploader: str = "未知 UP 主"
    desc: str = "暂无简介"
    duration: int = 0
    comments: list[BilibiliComment] = Field(default_factory=list)

    def format_comments(self, count: int, char_limit: int) -> str:
        if not self.comments:
            return "暂无评论数据"
        lines: list[str] = []
        for index, comment in enumerate(self.comments[:count], start=1):
            message = comment.message.replace("\n", " ")
            if len(message) > char_limit:
                message = f"{message[:char_limit]}……"
            lines.append(f"【{index}】{message}（点赞：{comment.like}）")
        return "\n".join(lines) or "暂无评论数据"

