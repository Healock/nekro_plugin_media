from ..matcher_cleanup import destroy_matcher
from .matcher import douyin_interceptor


async def cleanup_matcher() -> None:
    destroy_matcher(douyin_interceptor)
