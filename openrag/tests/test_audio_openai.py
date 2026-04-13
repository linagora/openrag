"""
Unit tests for openai audio processing functionality (./openai.py).

These tests validate the pydub operations used in openai.py without
importing the full module (which has complex dependencies).
"""

import warnings

# Filter pydub warnings before importing
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydub")

import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from pydub import AudioSegment  # noqa: E402
from pydub.generators import Sine  # noqa: E402


class TestAudioSegmentOperations:
    """Test pydub AudioSegment operations used in openai."""

    def test_create_audio_segment(self):
        """Test creating an audio segment."""
        # Create a 1 second sine wave at 440Hz
        audio = Sine(440).to_audio_segment(duration=1000)
        assert len(audio) == 1000  # 1000ms

    def test_audio_duration(self):
        """Test getting audio duration in milliseconds."""
        audio = AudioSegment.silent(duration=500)  # 500ms of silence
        assert len(audio) == 500

    def test_audio_slicing(self):
        """Test slicing audio by milliseconds."""
        audio = Sine(440).to_audio_segment(duration=2000)  # 2 seconds

        # Slice first second
        first_half = audio[:1000]
        assert len(first_half) == 1000

        # Slice second half
        second_half = audio[1000:]
        assert len(second_half) == 1000

        # Slice middle portion
        middle = audio[500:1500]
        assert len(middle) == 1000

    def test_set_channels_mono(self):
        """Test converting to mono."""
        # Create stereo audio
        stereo = Sine(440).to_audio_segment(duration=1000)
        stereo = stereo.set_channels(2)
        assert stereo.channels == 2

        # Convert to mono
        mono = stereo.set_channels(1)
        assert mono.channels == 1

    def test_set_frame_rate(self):
        """Test changing sample rate."""
        audio = Sine(440).to_audio_segment(duration=1000)

        # Downsample to 16kHz
        downsampled = audio.set_frame_rate(16000)
        assert downsampled.frame_rate == 16000

    def test_export_and_load_wav(self):
        """Test exporting and loading WAV files."""
        audio = Sine(440).to_audio_segment(duration=1000)

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "test.wav"

            # Export
            audio.export(wav_path, format="wav")
            assert wav_path.exists()

            # Load back
            loaded = AudioSegment.from_wav(wav_path)
            assert len(loaded) == 1000

    def test_export_and_load_mp3(self):
        """Test exporting and loading MP3 files (if ffmpeg available)."""
        audio = Sine(440).to_audio_segment(duration=1000)

        with tempfile.TemporaryDirectory() as tmpdir:
            mp3_path = Path(tmpdir) / "test.mp3"

            try:
                # Export as MP3
                audio.export(mp3_path, format="mp3")
                assert mp3_path.exists()

                # Load back
                loaded = AudioSegment.from_file(mp3_path, format="mp3")
                # MP3 may have slight duration differences due to encoding
                assert abs(len(loaded) - 1000) < 100
            except (FileNotFoundError, OSError):
                # Skip if ffmpeg not available
                pytest.skip("ffmpeg not available for MP3 encoding")


class TestSilenceDetection:
    """Test pydub silence detection used in openai."""

    def test_detect_silence_all_silent(self):
        """Test detection when entire audio is silent."""
        from pydub import silence

        audio = AudioSegment.silent(duration=1000)
        silences = silence.detect_silence(audio, min_silence_len=100, silence_thresh=-40)

        # Should detect one long silence
        assert len(silences) >= 1
        assert silences[0][0] == 0

    def test_detect_silence_no_silence(self):
        """Test detection when there is no silence."""
        from pydub import silence

        # Loud sine wave
        audio = Sine(440).to_audio_segment(duration=1000)

        silences = silence.detect_silence(audio, min_silence_len=100, silence_thresh=-40)

        # Should not detect any silence
        assert len(silences) == 0

    def test_detect_silence_in_middle(self):
        """Test detection of silence in the middle of audio."""
        from pydub import silence

        # Loud - Silent - Loud pattern
        loud = Sine(440).to_audio_segment(duration=500)
        silent = AudioSegment.silent(duration=300)

        audio = loud + silent + loud

        silences = silence.detect_silence(audio, min_silence_len=100, silence_thresh=-40)

        # Should detect one silence segment
        assert len(silences) >= 1
        # Silence should start around 500ms
        assert 400 <= silences[0][0] <= 600

    def test_detect_multiple_silences(self):
        """Test detection of multiple silence segments."""
        from pydub import silence

        # Pattern: loud-silent-loud-silent-loud
        loud = Sine(440).to_audio_segment(duration=300)
        silent = AudioSegment.silent(duration=200)

        audio = loud + silent + loud + silent + loud

        silences = silence.detect_silence(audio, min_silence_len=100, silence_thresh=-40)

        # Should detect two silence segments
        assert len(silences) >= 2

    def test_short_silence_ignored(self):
        """Test that silences shorter than min_silence_len are ignored."""
        from pydub import silence

        # Loud with very short silence (50ms)
        loud = Sine(440).to_audio_segment(duration=500)
        short_silent = AudioSegment.silent(duration=50)

        audio = loud + short_silent + loud

        # Request minimum 200ms silence
        silences = silence.detect_silence(audio, min_silence_len=200, silence_thresh=-40)

        # Should not detect the short silence
        assert len(silences) == 0


class TestAudioChunking:
    """Test chunking logic similar to openai._get_audio_chunks."""

    def get_audio_chunks(
        self,
        sound: AudioSegment,
        max_chunk_ms: int,
        min_silence_len_ms: int,
        silence_thresh_db: int,
    ) -> list:
        """
        Reproduce chunking logic from openai.
        """
        from pydub import silence

        total_ms = len(sound)
        if total_ms <= max_chunk_ms:
            return [(0, total_ms)]

        downsampled_sound = sound.set_channels(1).set_frame_rate(16000)
        silences = silence.detect_silence(
            downsampled_sound,
            min_silence_len=min_silence_len_ms,
            silence_thresh=silence_thresh_db,
        )

        chunks = []
        start = 0
        while start < total_ms:
            target_end = start + max_chunk_ms
            if target_end >= total_ms:
                end = total_ms
            else:
                valid_silences = [s for s in silences if start < s[0] < target_end]
                if valid_silences:
                    end = valid_silences[-1][0]
                else:
                    end = target_end
            chunks.append((start, end))
            start = end

        return chunks

    def test_short_audio_single_chunk(self):
        """Test that short audio results in single chunk."""
        audio = Sine(440).to_audio_segment(duration=5000)  # 5 seconds

        chunks = self.get_audio_chunks(
            audio,
            max_chunk_ms=10000,  # 10 seconds max
            min_silence_len_ms=500,
            silence_thresh_db=-40,
        )

        assert len(chunks) == 1
        assert chunks[0] == (0, 5000)

    def test_long_audio_multiple_chunks(self):
        """Test that long audio is split into multiple chunks."""
        # Create 10 seconds of audio with silences
        segment = Sine(440).to_audio_segment(duration=2000) + AudioSegment.silent(duration=500)
        audio = segment * 3  # Repeat pattern

        chunks = self.get_audio_chunks(
            audio,
            max_chunk_ms=3000,  # 3 seconds max per chunk
            min_silence_len_ms=200,
            silence_thresh_db=-40,
        )

        # Should have multiple chunks
        assert len(chunks) >= 2

        # All chunks should be within max size (with some tolerance)
        for start, end in chunks[:-1]:  # Except last chunk
            assert end - start <= 3500  # Allow some tolerance

    def test_chunks_cover_entire_audio(self):
        """Test that chunks cover the entire audio without gaps."""
        audio = Sine(440).to_audio_segment(duration=10000)  # 10 seconds

        chunks = self.get_audio_chunks(
            audio,
            max_chunk_ms=3000,
            min_silence_len_ms=500,
            silence_thresh_db=-40,
        )

        # First chunk should start at 0
        assert chunks[0][0] == 0

        # Last chunk should end at audio length
        assert chunks[-1][1] == 10000

        # Chunks should be contiguous
        for i in range(len(chunks) - 1):
            assert chunks[i][1] == chunks[i + 1][0]
