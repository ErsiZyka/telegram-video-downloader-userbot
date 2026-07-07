import pytest
from bot.downloader import (
    QUALITY_FORMATS,
    format_size,
    format_speed,
    format_eta,
)


class TestQualityFormats:
    def test_360_format(self):
        fmt = QUALITY_FORMATS["360"]
        assert "height<=360" in fmt
        assert "bestvideo" in fmt
        assert "bestaudio" in fmt

    def test_720_format(self):
        fmt = QUALITY_FORMATS["720"]
        assert "height<=720" in fmt

    def test_1080_format(self):
        fmt = QUALITY_FORMATS["1080"]
        assert "height<=1080" in fmt

    def test_max_format(self):
        fmt = QUALITY_FORMATS["max"]
        assert "bestvideo+bestaudio" in fmt

    def test_all_keys_present(self):
        assert set(QUALITY_FORMATS.keys()) == {"360", "720", "1080", "max"}


class TestFormatSize:
    def test_zero_bytes(self):
        assert format_size(0) == "0.0 MB"

    def test_one_mb(self):
        assert format_size(1) == "1.0 MB"

    def test_one_gb(self):
        result = format_size(1024)
        assert "GB" in result
        assert "1.0" in result

    def test_small_fraction(self):
        result = format_size(0.5)
        assert "0.5 MB" == result


class TestFormatSpeed:
    def test_zero_speed(self):
        assert format_speed(0) == "0.0 MB/s"

    def test_typical_speed(self):
        result = format_speed(12.5)
        assert "12.5 MB/s" == result


class TestFormatEta:
    def test_zero_seconds(self):
        assert format_eta(0) == "0s"

    def test_seconds_only(self):
        assert format_eta(45) == "45s"

    def test_minutes_and_seconds(self):
        result = format_eta(125)
        assert result == "2m 5s"

    def test_hours(self):
        result = format_eta(3661)
        assert result == "1h 1m 1s"

    def test_negative_eta(self):
        assert format_eta(-1) == "..."

    def test_none_eta(self):
        assert format_eta(None) == "..."


from unittest.mock import patch, MagicMock
from bot.downloader import extract_info, ExtractError


class TestExtractInfo:
    def test_single_video_returns_dict(self):
        mock_info = {
            "title": "Test Video",
            "duration": 120,
            "thumbnail": "https://example.com/thumb.jpg",
            "webpage_url": "https://youtube.com/watch?v=test",
            "uploader": "TestChannel",
        }
        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = extract_info("https://youtube.com/watch?v=test")
            assert isinstance(result, dict)
            assert result["title"] == "Test Video"

    def test_playlist_returns_list(self):
        mock_info = {
            "entries": [
                {"title": "Video 1", "webpage_url": "https://..."},
                {"title": "Video 2", "webpage_url": "https://..."},
            ],
            "title": "My Playlist",
        }
        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = extract_info("https://youtube.com/playlist?list=test")
            assert isinstance(result, list)
            assert len(result) == 2
            assert result[0]["title"] == "Video 1"

    def test_extract_error_raises_extract_error(self):
        from yt_dlp.utils import DownloadError as YTDLDownloadError

        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.side_effect = YTDLDownloadError("test error")
            with pytest.raises(ExtractError):
                extract_info("https://invalid.url")

    def test_empty_playlist_returns_empty_list(self):
        mock_info = {"entries": [], "title": "Empty Playlist"}
        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl:
            mock_ydl.return_value.__enter__.return_value.extract_info.return_value = mock_info
            result = extract_info("https://youtube.com/playlist?list=empty")
            assert result == []
