from ..matcher_cleanup import destroy_matcher
from .matcher import bili_interceptor


async def cleanup_matcher() -> None:
    destroy_matcher(bili_interceptor)
