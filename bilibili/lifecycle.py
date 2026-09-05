from nonebot.matcher import matchers

from .matcher import bili_interceptor


async def cleanup_matcher() -> None:
    for priority, registered in list(matchers.items()):
        remaining = [item for item in registered if item is not bili_interceptor]
        if remaining:
            matchers[priority] = remaining
        else:
            matchers.pop(priority, None)
