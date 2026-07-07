import pytest
from bot.handlers import (
    URL_REGEX,
    extract_url,
    _escape_md,
    QUALITY_CHOICES,
)


class TestUrlRegex:
    def test_extract_http_url(self):
        result = extract_url("Guarda https://youtube.com/watch?v=abc123")
        assert result == "https://youtube.com/watch?v=abc123"

    def test_extract_https_url(self):
        result = extract_url("https://www.tiktok.com/@user/video/123")
        assert result == "https://www.tiktok.com/@user/video/123"

    def test_extract_url_with_query_params(self):
        result = extract_url("https://example.com/video?id=1&quality=hd testo")
        assert result == "https://example.com/video?id=1&quality=hd"

    def test_no_url_returns_none(self):
        assert extract_url("Ciao, nessun link qui!") is None

    def test_empty_string(self):
        assert extract_url("") is None

    def test_multiple_urls_extracts_first(self):
        result = extract_url("https://a.com primo https://b.com secondo")
        assert result == "https://a.com"

    def test_url_without_scheme_not_matched(self):
        result = extract_url("vai su youtube.com/watch?v=test")
        assert result is None

    def test_instagram_url(self):
        result = extract_url("https://www.instagram.com/reel/ABC123/ guarda")
        assert result == "https://www.instagram.com/reel/ABC123/"

    def test_twitter_url(self):
        result = extract_url("https://x.com/user/status/123456")
        assert result == "https://x.com/user/status/123456"


class TestEscapeMarkdown:
    def test_plain_text_unchanged(self):
        assert _escape_md("Titolo normale") == "Titolo normale"

    def test_escape_asterisk(self):
        assert _escape_md("Video **promo**") == "Video \\*\\*promo\\*\\*"

    def test_escape_underscore(self):
        assert _escape_md("my_title") == "my\\_title"

    def test_escape_backtick(self):
        assert _escape_md("code`here") == "code\\`here"

    def test_escape_brackets(self):
        assert _escape_md("[link]") == "\\[link\\]"

    def test_escape_backslash_first(self):
        assert _escape_md("a\\b*c") == "a\\\\b\\*c"

    def test_empty_string(self):
        assert _escape_md("") == ""

    def test_none_like(self):
        assert _escape_md(None) == ""


class TestQualityChoices:
    def test_four_choices(self):
        assert len(QUALITY_CHOICES) == 4

    def test_choice_1_is_360(self):
        label, quality = QUALITY_CHOICES["1"]
        assert quality == "360"
        assert "360" in label

    def test_choice_4_is_max(self):
        label, quality = QUALITY_CHOICES["4"]
        assert quality == "max"
        assert "MAX" in label.upper()

    def test_all_qualities_present(self):
        qualities = [v[1] for v in QUALITY_CHOICES.values()]
        assert set(qualities) == {"360", "720", "1080", "max"}
