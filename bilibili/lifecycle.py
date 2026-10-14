from ..matcher_cleanup import destroy_matcher
from . import matcher


async def cleanup_matcher() -> None:
    destroy_matcher(matcher.bili_interceptor)
    matcher.bili_interceptor = None
