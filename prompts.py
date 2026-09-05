from __future__ import annotations

from .types import MediaType


def build_media_prompt(
    media_type: MediaType | str,
    sender_name: str,
    context_text: str = "",
    user_focus: str = "",
) -> str:
    kind = MediaType(media_type)
    if kind is MediaType.AUDIO:
        task = "转写音频内容，并概括说话者的音色、口音、语气、情绪及环境音。"
        output = "使用简洁客观的纯文本；只陈述音频中可确认的信息，不猜测背景。"
    else:
        task = "描述画面中的物体、环境变化、文字界面及对白音频，概括可确认的内容主线。"
        output = "使用简洁客观的纯文本；区分画面、声音和文字信息，不进行主观臆测。"

    lines = [
        "【任务】",
        task,
        "【输出要求】",
        output,
    ]
    if kind is MediaType.VIDEO and _is_bilibili_context(context_text):
        lines.append("如来源包含长流媒体信息，另加一节【核心总结】，结合标题和评论提炼主旨。")
    lines.extend(["【来源】", sender_name or "群友"])
    if context_text.strip():
        lines.extend(["【上下文】", context_text.strip()])
    else:
        lines.extend(["【上下文】", "无"])
    lines.extend(["【用户关注点】", user_focus.strip() or "无；按任务完成客观解析。"])
    return "\n".join(lines)


def _is_bilibili_context(context_text: str) -> bool:
    lowered = context_text.casefold()
    return any(marker in lowered for marker in ("b站", "bilibili", "b23.tv"))
