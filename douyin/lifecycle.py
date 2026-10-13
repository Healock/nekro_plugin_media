from ..matcher_cleanup import destroy_matcher
from . import matcher


async def cleanup_matcher() -> None:
    destroy_matcher(matcher.douyin_interceptor)
    matcher.douyin_interceptor = None
