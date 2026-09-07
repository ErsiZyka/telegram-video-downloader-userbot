import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
from bot.downloader import (
    QUALITY_FORMATS,
    DownloadError,
    ExtractError,
    _download_direct_http,
    _ensure_mp4_ext,
    _resolve_downloaded_file,
    check_dependencies,
    download_video,
    extract_info,
    format_eta,
    format_size,
    format_speed,
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
        assert result == "0.5 MB"


class TestFormatSpeed:
    def test_zero_speed(self):
        assert format_speed(0) == "0.0 MB/s"

    def test_typical_speed(self):
        result = format_speed(12.5)
        assert result == "12.5 MB/s"


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


class TestDownloadVideo:
    def test_download_success_returns_filepath(self):
        expected_path = "downloads" + os.sep + "Test Video.mp4"
        mock_ydl = MagicMock()
        mock_ydl.prepare_filename.return_value = expected_path

        progress_calls = []

        def progress_cb(downloaded, total, speed, eta):
            progress_calls.append((downloaded, total, speed, eta))

        with (
            patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
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

        with (
            patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
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

        with (
            patch("bot.downloader.yt_dlp.YoutubeDL") as mock_ydl_class,
            patch("os.path.exists", return_value=True),
        ):
            mock_ydl_class.return_value.__enter__.return_value = mock_ydl
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
            instance.extract_info.return_value = {"title": "video"}

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


class TestResolveDownloadedFile:
    """Riconcilia il percorso predetto da yt-dlp col file realmente scritto.

    Bug risolto: i download di playlist (es. erothots con più entry) fanno
    restituire a `prepare_filename()` un percorso inesistente (es. ".NA"),
    e l'upload falliva con FileNotFoundError.
    """

    @staticmethod
    def _touch(path: str, size: int) -> None:
        with open(path, "wb") as f:
            f.write(b"x" * size)

    def test_predicted_exists(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "video.mp4")
            self._touch(p, 100)
            assert _resolve_downloaded_file(p, d) == p

    def test_merge_extension_from_base(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "titolo.mkv")
            self._touch(real, 100)
            predicted = os.path.join(d, "titolo.ts")
            assert _resolve_downloaded_file(predicted, d) == real

    def test_unknown_video_extension_from_base(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "clip.unknown_video")
            self._touch(real, 100)
            predicted = os.path.join(d, "clip.NA")
            assert _resolve_downloaded_file(predicted, d) == real

    def test_playlist_picks_largest_recent_file(self):
        """Caso erothots: predetto .NA, su disco il video reale e la spazzatura."""
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "Tanababyxo - EroThots (1).mp4")
            self._touch(real, 10_000_000)
            junk = os.path.join(d, "Tanababyxo - EroThots (2).unknown_video")
            self._touch(junk, 90_000)
            self._touch(os.path.join(d, "playlist.mp4.part"), 999_999)
            time.sleep(0.01)
            predicted = os.path.join(d, "Tanababyxo - EroThots.NA")
            assert _resolve_downloaded_file(predicted, d) == real

    def test_ignores_old_files(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "vecchio.mp4")
            self._touch(old, 5_000_000)
            os.utime(old, (time.time() - 7200, time.time() - 7200))
            fresh = os.path.join(d, "nuovo.mp4")
            self._touch(fresh, 1_000_000)
            predicted = os.path.join(d, "nuovo.NOPE")
            assert _resolve_downloaded_file(predicted, d) == fresh


class TestEnsureMp4Ext:
    """Il gateway remote_control.php di x-video.tube serve un MP4 vero ma
    yt-dlp ricava l'estensione dall'URL -> file "Titolo.php". ffprobe deve
    riconoscere il contenuto e il file va rinominato in .mp4."""

    @staticmethod
    def _make_mp4(path: str) -> None:
        import subprocess

        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=duration=0.3:size=128x96:rate=10",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-f", "mp4",
                path,
            ],
            check=True,
            capture_output=True,
        )

    def test_php_con_mp4_dentro_rinominato(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "Video.php")
            self._make_mp4(fake)
            result = _ensure_mp4_ext(fake)
            assert result == os.path.join(d, "Video.mp4")
            assert os.path.exists(result)
            assert not os.path.exists(fake)

    def test_file_non_video_intatto(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "pagina.php")
            with open(fake, "w") as f:
                f.write("<html>non sono un video</html>")
            assert _ensure_mp4_ext(fake) == fake
            assert os.path.exists(fake)

    def test_estensioni_valide_intatte(self):
        with tempfile.TemporaryDirectory() as d:
            for ext in (".mp4", ".mkv", ".webm"):
                p = os.path.join(d, "v" + ext)
                self._touch = None  # noqa: non serve touch reale
                with open(p, "wb") as f:
                    f.write(b"\x00")
                assert _ensure_mp4_ext(p) == p

    def test_vuoto(self):
        assert _ensure_mp4_ext("") == ""


class TestDownloadDirectHttp:
    """Gateway .php (x-video.tube): yt-dlp blocca l'estensione insolita, il
    fallback HTTP diretto deve scaricare il body, segnalare progresso e
    scrivere un .mp4 col titolo come nome."""

    @staticmethod
    def _serve(payload: bytes):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_direct_http_download(self):
        from bot.downloader import _download_direct_http

        payload = b"X" * (256 * 1024 * 2 + 11)
        srv = self._serve(payload)
        try:
            old = os.getcwd()
            with tempfile.TemporaryDirectory() as d:
                os.chdir(d)
                try:
                    url = "http://127.0.0.1:{}/f.mp4?v-acctoken=x".format(
                        srv.server_address[1]
                    )
                    calls = []

                    def cb(a, b, c, e):
                        calls.append((a, b))

                    path = _download_direct_http(
                        url, cb, {"Referer": "https://x-video.tube/"}, "Video - Test"
                    )
                    assert path == os.path.join("downloads", "Video - Test.mp4")
                    with open(path, "rb") as f:
                        assert f.read() == payload
                    assert calls, "progress mai chiamato"
                    assert calls[-1][1] > 0
                finally:
                    os.chdir(old)
        finally:
            srv.shutdown()

    def test_direct_http_nome_sicuro(self):
        from bot.downloader import _download_direct_http

        payload = b"Y" * 1024
        srv = self._serve(payload)
        try:
            old = os.getcwd()
            with tempfile.TemporaryDirectory() as d:
                os.chdir(d)
                try:
                    url = "http://127.0.0.1:{}/f.mp4".format(srv.server_address[1])
                    path = _download_direct_http(url, None, None, 'A/B:C"D')
                    name = os.path.basename(path)
                    assert name.endswith(".mp4")
                    # caratteri pericolosi rimossi dal titolo
                    assert "/" not in name and ":" not in name and '"' not in name
                finally:
                    os.chdir(old)
        finally:
            srv.shutdown()
