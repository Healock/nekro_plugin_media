from __future__ import annotations

import pytest

from nekro_plugin_media.matcher_cleanup import destroy_matcher


class Matcher:
    def __init__(self) -> None:
        self.destroy_count = 0

    def destroy(self) -> None:
        self.destroy_count += 1


@pytest.mark.parametrize("error", [KeyError, ValueError])
def test_destroy_ignores_matcher_already_removed(error: type[Exception]) -> None:
    class RemovedMatcher:
        def destroy(self) -> None:
            raise error("not registered")

    destroy_matcher(RemovedMatcher())


def test_destroy_calls_matcher_instance() -> None:
    matcher = Matcher()

    destroy_matcher(matcher)

    assert matcher.destroy_count == 1
