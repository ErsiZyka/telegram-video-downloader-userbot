import pytest
from bot.handlers import URL_REGEX, extract_url


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


from bot.handlers import build_quality_keyboard, build_playlist_keyboard, parse_callback_data


class TestQualityKeyboard:
    def test_all_quality_buttons_present(self):
        keyboard = build_quality_keyboard("https://test.url")
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        labels = [b.text for b in buttons]
        assert "360p" in labels
        assert "720p" in labels
        assert "1080p" in labels
        assert any("MAX" in l for l in labels)
        assert "❌ Annulla" in labels

    def test_cancel_button_has_cancel_callback(self):
        keyboard = build_quality_keyboard("https://test.url")
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        cancel_btn = [b for b in buttons if b.text == "❌ Annulla"][0]
        assert cancel_btn.callback_data == "cancel"


class TestPlaylistKeyboard:
    def test_small_playlist_no_pagination(self):
        videos = [
            {"title": f"Video {i}", "webpage_url": f"https://test/{i}"}
            for i in range(5)
        ]
        keyboard = build_playlist_keyboard(videos, page=0)
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        assert len([b for b in buttons if "Video" in (b.text or "")]) == 5
        pagination_labels = [b.text for b in buttons if "◀️" in (b.text or "") or "▶️" in (b.text or "") or "Precedenti" in (b.text or "") or "Avanti" in (b.text or "")]
        assert len(pagination_labels) == 0

    def test_large_playlist_has_pagination(self):
        videos = [
            {"title": f"Video {i}", "webpage_url": f"https://test/{i}"}
            for i in range(25)
        ]
        keyboard = build_playlist_keyboard(videos, page=0)
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        texts = [b.text for b in buttons]
        assert "Avanti ▶️" in texts
        assert "◀️ Precedenti" not in texts

    def test_playlist_page_1_has_both_nav(self):
        videos = [{"title": f"V{i}", "webpage_url": f"https://test/{i}"} for i in range(25)]
        keyboard = build_playlist_keyboard(videos, page=1)
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        texts = [b.text for b in buttons]
        assert "◀️ Precedenti" in texts
        assert "Avanti ▶️" in texts


class TestCallbackParsing:
    def test_quality_callback(self):
        # Format: quality:QUALITY|URL
        action, param1, param2 = parse_callback_data("quality:720|https://test.url")
        assert action == "quality"
        assert param1 == "720"  # quality value
        assert param2 == "https://test.url"  # url

    def test_playlist_callback(self):
        # Format: playlist:URL|index
        action, param1, param2 = parse_callback_data("playlist:https://test/pl|3")
        assert action == "playlist"
        assert param1 == "https://test/pl"
        assert param2 == "3"

    def test_cancel_callback(self):
        action, param1, param2 = parse_callback_data("cancel")
        assert action == "cancel"
        assert param1 is None
        assert param2 is None

    def test_page_callback(self):
        action, param1, param2 = parse_callback_data("page:2")
        assert action == "page"
        assert param1 == "2"
        assert param2 is None
