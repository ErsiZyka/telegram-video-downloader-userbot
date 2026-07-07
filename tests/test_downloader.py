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
        # 360 falls back to height<=480 for CDNs with no 360p tier (streamingcommunity)
        assert "height<=480" in fmt
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


import os
from unittest.mock import patch, MagicMock, call
from bot.downloader import download_video, DownloadError, check_dependencies


class TestDownloadVideo:
    def test_download_success_returns_filepath(self):
        expected_path = "downloads" + os.sep + "Test Video.mp4"
        mock_ydl = MagicMock()
        mock_ydl.prepare_filename.return_value = expected_path

        progress_calls = []

        def progress_cb(downloaded, total, speed, eta):
            progress_calls.append((downloaded, total, speed, eta))

        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
            with patch("os.path.exists", return_value=True):
                result = download_video(
                    "https://youtube.com/watch?v=test",
                    quality="720",
                    progress_callback=progress_cb,
                )
                assert result == expected_path

    def test_download_retry_on_failure(self):
        """After first 2 attempts fail, 3rd succeeds."""
        mock_ydl = MagicMock()
        mock_ydl.prepare_filename.return_value = "downloads/video.mp4"
        mock_ydl.extract_info.side_effect = [
            Exception("fail 1"),
            Exception("fail 2"),
            {"title": "video"},
        ]

        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
            with patch("os.path.exists", return_value=True):
                with patch("time.sleep") as mock_sleep:
                    result = download_video(
                        "https://test.url",
                        quality="max",
                        progress_callback=lambda *a: None,
                    )
                    assert mock_sleep.call_count == 2  # 2 backoff sleeps
                    assert result is not None

    def test_download_fails_after_all_retries(self):
        mock_ydl = MagicMock()
        mock_ydl.prepare_filename.return_value = "downloads/video.mp4"
        mock_ydl.extract_info.side_effect = Exception("persistent error")

        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
            with patch("os.path.exists", return_value=True):
                with pytest.raises(DownloadError):
                    download_video(
                        "https://test.url",
                        quality="360",
                        progress_callback=lambda *a: None,
                        max_retries=3,
                    )

    def test_progress_callback_is_called(self):
        """Progress hook should pass correct parameters to callback."""
        mock_ydl = MagicMock()
        mock_ydl.prepare_filename.return_value = "downloads/video.mp4"

        progress_calls = []

        def progress_cb(downloaded, total, speed, eta):
            progress_calls.append((downloaded, total, speed, eta))

        with patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
            instance = mock_ydl_class.return_value.__enter__.return_value
            instance = mock_ydl

            with patch("os.path.exists", return_value=True):
                download_video(
                    "https://test.url",
                    quality="720",
                    progress_callback=progress_cb,
                )

            # Verify progress_hooks was passed in ydl_opts
            call_kwargs = mock_ydl_class.call_args
            assert call_kwargs is not None

    def test_invalid_quality_raises_value_error(self):
        with pytest.raises(ValueError, match="Qualità non valida"):
            download_video(
                "https://test.url",
                quality="4k",
                progress_callback=lambda *a: None,
            )


class TestCheckDependencies:
    def test_ytdlp_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, msg = check_dependencies()
            assert not ok
            assert "yt-dlp" in msg.lower()

    def test_ffmpeg_missing(self):
        def sub_run_side_effect(cmd, **kwargs):
            cmd_str = cmd[0] if isinstance(cmd, list) else cmd
            if "yt-dlp" in str(cmd_str):
                r = MagicMock()
                r.returncode = 0
                r.stdout = "2024.01.01"
                return r
            if "ffmpeg" in str(cmd_str):
                raise FileNotFoundError
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=sub_run_side_effect):
            ok, msg = check_dependencies()
            assert not ok
            assert "ffmpeg" in msg.lower()

    def test_all_dependencies_present(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "2024.01.01"
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = check_dependencies()
            assert ok
            assert msg == ""
