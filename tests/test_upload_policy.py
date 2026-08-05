import pytest

from yt_shorts import upload_policy


class TestMode:
    def test_absent_upload_block_is_api(self):
        assert upload_policy.mode({}) == "api"

    def test_upload_block_without_mode_is_api(self):
        assert upload_policy.mode({"upload": {"tags": ["x"]}}) == "api"

    def test_explicit_api_is_api(self):
        assert upload_policy.mode({"upload": {"mode": "api"}}) == "api"

    def test_manual_is_manual(self):
        assert upload_policy.mode({"upload": {"mode": "manual"}}) == "manual"

    def test_unexpected_value_falls_back_to_api(self):
        # Never accidentally block a real owned channel; validation is what
        # rejects a bad value at load time, not this predicate.
        assert upload_policy.mode({"upload": {"mode": "bogus"}}) == "api"

    def test_non_dict_upload_is_api(self):
        assert upload_policy.mode({"upload": None}) == "api"


class TestGuard:
    def test_is_render_only_matches_mode(self):
        assert upload_policy.is_render_only({"upload": {"mode": "manual"}}) is True
        assert upload_policy.is_render_only({}) is False

    def test_require_api_upload_is_a_noop_for_api(self):
        upload_policy.require_api_upload({})  # must not raise

    def test_require_api_upload_refuses_manual_with_the_shared_message(self):
        with pytest.raises(upload_policy.RenderOnlyError) as error:
            upload_policy.require_api_upload({"upload": {"mode": "manual"}})
        assert str(error.value) == upload_policy.RENDER_ONLY_MESSAGE
