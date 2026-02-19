import asyncio
import datetime
import logging
import math
import os
import re
import shutil
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Union

import discord
from yt_dlp.utils import (  # type: ignore[import-untyped]
    ContentTooShortError,
    YoutubeDLError,
)

from .constructs import Serializable
from .downloader import YtdlpResponseDict
from .exceptions import ExtractionError, InvalidDataError, MusicbotException
from .spotify import Spotify

if TYPE_CHECKING:
    from .downloader import Downloader
    from .filecache import AudioFileCache
    from .playlist import Playlist

    # Explicit compat with python 3.8
    AsyncFuture = asyncio.Future[Any]
    AsyncTask = asyncio.Task[Any]
else:
    AsyncFuture = asyncio.Future
    AsyncTask = asyncio.Task

GuildMessageableChannels = Union[
    discord.Thread,
    discord.TextChannel,
    discord.VoiceChannel,
    discord.StageChannel,
]

log = logging.getLogger(__name__)

# optionally using pymediainfo instead of ffprobe if presents
try:
    import pymediainfo  # type: ignore[import-untyped]
except ImportError:
    log.debug("module 'pymediainfo' not found, will fall back to ffprobe.")
    pymediainfo = None


class BasePlaylistEntry(Serializable):
    def __init__(self) -> None:
        """
        Manage a playable media reference and its meta data.
        Either a URL or a local file path that ffmpeg can use.
        """
        self.filename: str = ""
        self.downloaded_bytes: int = 0
        self.memory_data: Optional[bytes] = None
        self.memory_size: int = 0
        self.cache_busted: bool = False
        self._is_downloading: bool = False
        self._is_downloaded: bool = False
        self._waiting_futures: List[AsyncFuture] = []
        self._task_pool: Set[AsyncTask] = set()

    @property
    def start_time(self) -> float:
        """
        Time in seconds that is passed to ffmpeg -ss flag.
        """
        return 0

    @property
    def url(self) -> str:
        """
        Get a URL suitable for YoutubeDL to download, or likewise
        suitable for ffmpeg to stream or directly play back.
        """
        raise NotImplementedError

    @property
    def title(self) -> str:
        """
        Get a title suitable for display using any extracted info.
        """
        raise NotImplementedError

    @property
    def duration_td(self) -> datetime.timedelta:
        """
        Get this entry's duration as a timedelta object.
        The object may contain a 0 value.
        """
        raise NotImplementedError

    @property
    def is_downloaded(self) -> bool:
        """
        Get the entry's downloaded status.
        Typically set by _download function.
        """
        if self._is_downloading:
            return False

        return bool(self.filename) and self._is_downloaded

    @property
    def is_downloading(self) -> bool:
        """Get the entry's downloading status. Usually False."""
        return self._is_downloading

    async def _download(self) -> None:
        """
        Take any steps needed to download the media and make it ready for playback.
        If the media already exists, this function can return early.
        """
        raise NotImplementedError

    def get_ready_future(self) -> AsyncFuture:
        """
        Returns a future that will fire when the song is ready to be played.
        The future will either fire with the result (being the entry) or an exception
        as to why the song download failed.
        """
        future: AsyncFuture = asyncio.Future()
        if self.is_downloaded:
            # In the event that we're downloaded, we're already ready for playback.
            future.set_result(self)

        else:
            # If we request a ready future, let's ensure that it'll actually resolve at one point.
            self._waiting_futures.append(future)
            task = asyncio.create_task(self._download(), name="MB_EntryReadyTask")
            # Make sure garbage collection does not delete the task early...
            self._task_pool.add(task)
            task.add_done_callback(self._task_pool.discard)

        log.debug("Created future for %r", self)
        return future

    def _for_each_future(self, cb: Callable[..., Any]) -> None:
        """
        Calls `cb` for each future that is not canceled.
        Absorbs and logs any errors that may have occurred.
        """
        futures = self._waiting_futures
        self._waiting_futures = []

        log.everything(  # type: ignore[attr-defined]
            "Completed futures for %r with %r", self, cb
        )
        for future in futures:
            if future.cancelled():
                continue

            try:
                cb(future)
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception("Unhandled exception in _for_each_future callback.")

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"<{type(self).__name__}(url='{self.url}', title='{self.title}' file='{self.filename}')>"


async def run_command(command: List[str]) -> bytes:
    """
    Use an async subprocess exec to execute the given `command`
    This method will wait for then return the output.

    :param: command:
        Must be a list of arguments, where element 0 is an executable path.

    :returns:  stdout concatenated with stderr as bytes.
    """
    p = await asyncio.create_subprocess_exec(
        # The inconsistency between the various implements of subprocess, asyncio.subprocess, and
        # all the other process calling functions tucked into python is alone enough to be dangerous.
        # There is a time and place for everything, and this is not the time or place for shells.
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.noise(  # type: ignore[attr-defined]
        "Starting asyncio subprocess (%s) with command: %s", p, command
    )
    stdout, stderr = await p.communicate()
    return stdout + stderr


async def decode_to_pcm16(
    input_file: str, loudnorm_gain: float = 1.0
) -> Optional[bytes]:
    """
    Decode compressed audio (MP3, M4A, etc.) to raw PCM16 (S16LE) format.

    Discord requires 16-bit 48kHz stereo PCM for non-Opus audio sources.
    This function uses FFmpeg to decode the audio and optionally applies
    loudnorm equalization during the decode process.

    :param input_file: Path to the compressed audio file
    :param loudnorm_gain: Linear gain multiplier (1.0 = no change)
        If != 1.0, loudnorm filter is applied during decode
    :returns: Raw PCM16 bytes or None if decoding fails

    Output format: 48kHz, stereo, 16-bit signed little-endian PCM
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        log.error("Could not locate ffmpeg on your path!")
        return None

    # Base FFmpeg command for PCM16 output
    ffmpeg_cmd = [
        ffmpeg_bin,
        "-i",
        input_file,
        "-ar",
        "48000",  # Sample rate: 48kHz (Discord requirement)
        "-ac",
        "2",  # Channels: stereo
        "-f",
        "s16le",  # Format: 16-bit signed little-endian PCM
    ]

    # Add loudnorm filter if EQ is enabled
    if loudnorm_gain != 1.0:
        # Convert linear gain back to dB offset for loudnorm
        offset_db = 20 * math.log10(loudnorm_gain)
        loudnorm_filter = (
            f"loudnorm=I=-24.0:LRA=7.0:TP=-2.0:linear=true:offset={offset_db:.2f}"
        )
        ffmpeg_cmd.extend(["-af", loudnorm_filter])
        log.debug(
            "Decoding with loudnorm EQ: gain=%.4f, offset=%.2f dB",
            loudnorm_gain,
            offset_db,
        )
    else:
        log.debug("Decoding without EQ (gain=1.0)")

    # Output to stdout
    ffmpeg_cmd.append("pipe:1")

    try:
        p = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await p.communicate()

        if p.returncode != 0:
            log.error(
                "FFmpeg decode failed with code %d: %s",
                p.returncode,
                stderr.decode("utf-8"),
            )
            return None

        if not stdout:
            log.error("FFmpeg produced no output for: %s", input_file)
            return None

        log.debug("Successfully decoded %s to PCM16: %d bytes", input_file, len(stdout))
        return stdout

    except Exception as e:
        log.error("Failed to decode audio to PCM16: %s", e, exc_info=True)
        return None


class URLPlaylistEntry(BasePlaylistEntry):
    SERIAL_VERSION: int = 3  # version for serial data checks.

    def __init__(
        self,
        playlist: "Playlist",
        info: YtdlpResponseDict,
        author: Optional["discord.Member"] = None,
        channel: Optional[GuildMessageableChannels] = None,
    ) -> None:
        """
        Create URL Playlist entry that will be downloaded for playback.

        :param: playlist:  The playlist object this entry should belong to.
        :param: info:  A YtdlResponseDict from downloader.extract_info()
        """
        super().__init__()

        self._start_time: Optional[float] = None
        self._playback_rate: Optional[float] = None
        self._loudnorm_gain: float = 1.0  # Gain multiplier from loudnorm EQ
        self.playlist: "Playlist" = playlist
        self.downloader: "Downloader" = playlist.bot.downloader
        self.filecache: "AudioFileCache" = playlist.bot.filecache

        self.info: YtdlpResponseDict = info

        if self.duration is None:
            log.info(
                "Extraction did not provide a duration for this entry.\n"
                "MusicBot cannot estimate queue times until it is downloaded.\n"
                "Entry name:  %s",
                self.title,
            )

        self.author: Optional["discord.Member"] = author
        self.channel: Optional[GuildMessageableChannels] = channel

        self._aopt_eq: str = ""

    @property
    def aoptions(self) -> str:
        """After input options for ffmpeg to use with this entry."""
        aopts = f"{self._aopt_eq}"
        # Set playback speed options if needed.
        if self._playback_rate is not None or self.playback_speed != 1.0:
            # Append to the EQ options if they are set.
            if self._aopt_eq:
                aopts = f"{self._aopt_eq},atempo={self.playback_speed:.3f}"
            else:
                aopts = f"-af atempo={self.playback_speed:.3f}"

        if aopts:
            return f"{aopts} -vn"

        return "-vn"

    @property
    def boptions(self) -> str:
        """Before input options for ffmpeg to use with this entry."""
        if self._start_time is not None:
            return f"-ss {self._start_time}"
        return ""

    @property
    def from_auto_playlist(self) -> bool:
        """Returns true if the entry has an author or a channel."""
        if self.author is not None or self.channel is not None:
            return False
        return True

    @property
    def url(self) -> str:
        """Gets a playable URL from this entries info."""
        return self.info.get_playable_url()

    @property
    def title(self) -> str:
        """Gets a title string from entry info or 'Unknown'"""
        # TODO: i18n for this at some point.
        return self.info.title or "Unknown"

    @property
    def duration(self) -> Optional[float]:
        """Gets available duration data or None"""
        # duration can be 0, if so we make sure it returns None instead.
        return self.info.get("duration", None) or None

    @duration.setter
    def duration(self, value: float) -> None:
        self.info["duration"] = value

    @property
    def duration_td(self) -> datetime.timedelta:
        """
        Returns duration as a datetime.timedelta object.
        May contain 0 seconds duration.
        """
        t = self.duration or 0
        return datetime.timedelta(seconds=t)

    @property
    def thumbnail_url(self) -> str:
        """Get available thumbnail from info or an empty string"""
        return self.info.thumbnail_url

    @property
    def expected_filename(self) -> Optional[str]:
        """Get the expected filename from info if available or None"""
        return self.info.get("__expected_filename", None)

    def __json__(self) -> Dict[str, Any]:
        """
        Handles representing this object as JSON.
        """
        # WARNING:  if you change data or keys here, you must increase SERIAL_VERSION.
        return self._enclose_json(
            {
                "version": URLPlaylistEntry.SERIAL_VERSION,
                "info": self.info.data,
                "downloaded": self.is_downloaded,
                "filename": self.filename,
                "author_id": self.author.id if self.author else None,
                "channel_id": self.channel.id if self.channel else None,
                "aoptions": self.aoptions,
            }
        )

    @classmethod
    def _deserialize(
        cls,
        raw_json: Dict[str, Any],
        playlist: Optional["Playlist"] = None,
        **kwargs: Dict[str, Any],
    ) -> Optional["URLPlaylistEntry"]:
        """
        Handles converting from JSON to URLPlaylistEntry.
        """
        # WARNING:  if you change data or keys here, you must increase SERIAL_VERSION.

        # yes this is an Optional that is, in fact, not Optional. :)
        assert playlist is not None, cls._bad("playlist")

        vernum: Optional[int] = raw_json.get("version", None)
        if not vernum:
            log.error("Entry data is missing version number, cannot deserialize.")
            return None
        if vernum != URLPlaylistEntry.SERIAL_VERSION:
            log.error("Entry data has the wrong version number, cannot deserialize.")
            return None

        try:
            info = YtdlpResponseDict(raw_json["info"])
            downloaded = (
                raw_json["downloaded"] if playlist.bot.config.save_videos else False
            )
            filename = raw_json["filename"] if downloaded else None

            channel_id = raw_json.get("channel_id", None)
            if channel_id:
                o_channel = playlist.bot.get_channel(int(channel_id))

                if not o_channel:
                    log.warning(
                        "Deserialized URLPlaylistEntry cannot find channel with id:  %s",
                        raw_json["channel_id"],
                    )

                if isinstance(
                    o_channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    channel = o_channel
                else:
                    log.warning(
                        "Deserialized URLPlaylistEntry has the wrong channel type:  %s",
                        type(o_channel),
                    )
                    channel = None
            else:
                channel = None

            author_id = raw_json.get("author_id", None)
            if author_id:
                if isinstance(
                    channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    author = channel.guild.get_member(int(author_id))

                    if not author:
                        log.warning(
                            "Deserialized URLPlaylistEntry cannot find author with id:  %s",
                            raw_json["author_id"],
                        )
                else:
                    author = None
                    log.warning(
                        "Deserialized URLPlaylistEntry has an author ID but no channel for lookup!",
                    )
            else:
                author = None

            entry = cls(playlist, info, author=author, channel=channel)
            entry.filename = filename
            # Strip .json extension if present (yt-dlp may include it for metadata files)
            if entry.filename.endswith(".json"):
                entry.filename = entry.filename[:-5]

            # Set __expected_filename in info data so cache checks work correctly
            # This is needed because expected_filename property reads from info data
            if entry.filename:
                entry.info.data["__expected_filename"] = entry.filename

            return entry
        except (ValueError, TypeError, KeyError) as e:
            log.error("Could not load %s", cls.__name__, exc_info=e)

        return None

    @property
    def start_time(self) -> float:
        if self._start_time is not None:
            return self._start_time
        return 0

    def set_start_time(self, start_time: float) -> None:
        """Sets a start time in seconds to use with the ffmpeg -ss flag."""
        self._start_time = start_time

    @property
    def playback_speed(self) -> float:
        """Get the current playback speed if one was set, or return 1.0 for normal playback."""
        if self._playback_rate is not None:
            return self._playback_rate
        return self.playlist.bot.config.default_speed or 1.0

    def set_playback_speed(self, speed: float) -> None:
        """Set the playback speed to be used with ffmpeg -af:atempo filter."""
        self._playback_rate = speed

    async def _ensure_entry_info(self) -> None:
        """helper to ensure this entry object has critical information"""

        # handle some extra extraction here so we can allow spotify links in the queue.
        if "open.spotify.com" in self.url.lower() and Spotify.is_url_supported(
            self.url
        ):
            info = await self.downloader.extract_info(self.url, download=False)
            if info.ytdl_type == "url":
                self.info = info
            else:
                raise InvalidDataError(
                    f"Cannot download spotify links, processing error with type: {info.ytdl_type}."
                )

        # if this isn't set this entry is probably from a playlist and needs more info.
        if not self.expected_filename:
            new_info = await self.downloader.extract_info(self.url, download=False)
            self.info.data = {**self.info.data, **new_info.data}

    async def _download(self) -> None:
        if self._is_downloading:
            return
        log.debug("Getting ready for entry:  %r", self)

        self._is_downloading = True
        try:
            # Ensure any late-extraction links, like Spotify tracks, get processed.
            await self._ensure_entry_info()

            # Ensure the folder that we're going to move into exists.
            self.filecache.ensure_cache_dir_exists()

            # check and see if the expected file already exists in cache.
            if self.expected_filename:
                # get an existing cache path if we have one.
                file_cache_path = self.filecache.get_if_cached(self.expected_filename)

                # win a cookie if cache worked but extension was different.
                if file_cache_path and self.expected_filename != file_cache_path:
                    log.warning("Download cached with different extension...")

                # check if cache size matches remote, basic validation.
                if file_cache_path:
                    # Respect the configuration option to skip verification.
                    if self.playlist.bot.config.skip_cache_size_check:
                        log.debug(
                            "Cache size verification disabled; using cached file directly: %s",
                            file_cache_path,
                        )
                        self.filename = file_cache_path
                        self._is_downloaded = True
                    else:
                        local_size = os.path.getsize(file_cache_path)
                        remote_size = int(self.info.http_header("CONTENT-LENGTH", 0))

                        if local_size != remote_size:
                            log.debug(
                                "Local size different from remote size. Re-downloading..."
                            )
                            await self._really_download()
                        else:
                            log.debug(
                                "Download already cached at:  %s", file_cache_path
                            )
                            self.filename = file_cache_path
                            self._is_downloaded = True

                    # Load cached file into memory if config is enabled
                    if (
                        self.playlist.bot.config.load_audio_into_memory
                        and not self.memory_data
                    ):
                        # Estimate PCM16 size: compressed audio is typically ~10x smaller
                        estimated_pcm_size = os.path.getsize(self.filename) * 10
                        if not self.playlist.bot.filecache.has_memory_capacity(
                            estimated_pcm_size
                        ):
                            log.debug(
                                "Memory limit reached, skipping in-memory load for cached file: %s",
                                self.title,
                            )
                        else:
                            try:
                                log.debug(
                                    "Loading cached audio into memory: %s",
                                    self.title,
                                )
                                # Decode to PCM16, applying loudnorm EQ if enabled
                                loudnorm_gain = getattr(self, "_loudnorm_gain", 1.0)
                                pcm_data = await decode_to_pcm16(
                                    self.filename, loudnorm_gain
                                )
                                if pcm_data:
                                    self.memory_data = pcm_data
                                    self.memory_size = len(pcm_data)

                                    if self.playlist.bot.filecache.allocate_memory(
                                        self.memory_size
                                    ):
                                        log.debug(
                                            "Successfully loaded cached file into memory: %d bytes PCM16",
                                            self.memory_size,
                                        )
                                    else:
                                        self.memory_data = None
                                        self.memory_size = 0
                                else:
                                    log.error(
                                        "Failed to decode cached audio to PCM16: %s",
                                        self.title,
                                    )
                                    self.memory_data = None
                                    self.memory_size = 0
                            except Exception as e:
                                log.error(
                                    "Failed to load cached audio into memory: %s, error: %s",
                                    self.title,
                                    str(e),
                                )
                                self.memory_data = None
                                self.memory_size = 0

                # nothing cached, time to download for real.
                else:
                    await self._really_download()

            # check for duration and attempt to extract it if missing.
            if self.duration is None:
                # optional pymediainfo over ffprobe?
                if pymediainfo:
                    self.duration = self._get_duration_pymedia(self.filename)

                # no matter what, ffprobe should be available.
                if self.duration is None:
                    self.duration = await self._get_duration_ffprobe(self.filename)

                if not self.duration:
                    log.error(
                        "MusicBot could not get duration data for this entry.\n"
                        "Queue time estimation may be unavailable until this track is cleared.\n"
                        "Entry file: %s",
                        self.filename,
                    )
                else:
                    log.debug(
                        "Got duration of %s seconds for file:  %s",
                        self.duration,
                        self.filename,
                    )

            # Save metadata to sidecar file for cache retrieval
            if self.filename and self.playlist.bot.config.save_videos:
                metadata = {
                    "title": self.title,
                    "duration": self.duration,
                    "url": self.url,
                }
                self.filecache.save_metadata(self.filename, metadata)

            if self.playlist.bot.config.use_experimental_equalization:
                try:
                    self._aopt_eq = await self.get_mean_volume(self.filename)
                    if self._aopt_eq:
                        self._loudnorm_gain = await self.get_loudnorm_gain(
                            self.filename
                        )

                # Unfortunate evil that we abide for now...
                except Exception:  # pylint: disable=broad-exception-caught
                    log.error(
                        "There as a problem with working out EQ, likely caused by a strange installation of FFmpeg. "
                        "This has not impacted the ability for the bot to work, but will mean your tracks will not be equalised.",
                        exc_info=True,
                    )

            # Trigger ready callbacks.
            self._for_each_future(lambda future: future.set_result(self))

        # Flake8 thinks 'e' is never used, and later undefined. Maybe the lambda is too much.
        except Exception as e:  # pylint: disable=broad-exception-caught
            ex = e
            if log.getEffectiveLevel() <= logging.DEBUG:
                log.error("Exception while checking entry data.")
            self._for_each_future(lambda future: future.set_exception(ex))

        finally:
            self._is_downloading = False

    def _get_duration_pymedia(self, input_file: str) -> Optional[float]:
        """
        Tries to use pymediainfo module to extract duration, if the module is available.
        """
        if pymediainfo:
            log.debug("Trying to get duration via pymediainfo for:  %s", input_file)
            try:
                mediainfo = pymediainfo.MediaInfo.parse(input_file)
                if mediainfo.tracks:
                    return int(mediainfo.tracks[0].duration) / 1000
            except (FileNotFoundError, OSError, RuntimeError, ValueError, TypeError):
                log.exception("Failed to get duration via pymediainfo.")
        return None

    async def _get_duration_ffprobe(self, input_file: str) -> Optional[float]:
        """
        Tries to use ffprobe to extract duration from media if possible.
        """
        log.debug("Trying to get duration via ffprobe for:  %s", input_file)
        ffprobe_bin = shutil.which("ffprobe")
        if not ffprobe_bin:
            log.error("Could not locate ffprobe in your path!")
            return None

        ffprobe_cmd = [
            ffprobe_bin,
            "-i",
            self.filename,
            "-show_entries",
            "format=duration",
            "-v",
            "quiet",
            "-of",
            "csv=p=0",
        ]

        try:
            raw_output = await run_command(ffprobe_cmd)
            output = raw_output.decode("utf8")
            return float(output)
        except (ValueError, UnicodeError):
            log.error(
                "ffprobe returned something that could not be used.", exc_info=True
            )
        except Exception:  # pylint: disable=broad-exception-caught
            log.exception("ffprobe could not be executed for some reason.")

        return None

    async def get_mean_volume(self, input_file: str) -> str:
        """
        Attempt to calculate the mean volume of the `input_file` by using
        output from ffmpeg to provide values which can be used by command
        arguments sent to ffmpeg during playback.
        """
        log.debug("Calculating mean volume of:  %s", input_file)
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            log.error("Could not locate ffmpeg on your path!")
            return ""

        # NOTE: this command should contain JSON, but I have no idea how to make
        # ffmpeg spit out only the JSON.
        ffmpeg_cmd = [
            ffmpeg_bin,
            "-i",
            input_file,
            "-af",
            "loudnorm=I=-24.0:LRA=7.0:TP=-2.0:linear=true:print_format=json",
            "-f",
            "null",
            "/dev/null",
            "-hide_banner",
            "-nostats",
        ]

        raw_output = await run_command(ffmpeg_cmd)
        output = raw_output.decode("utf-8")

        i_matches = re.findall(r'"input_i" : "(-?([0-9]*\.[0-9]+))",', output)
        if i_matches:
            # log.debug("i_matches=%s", i_matches[0][0])
            i_value = float(i_matches[0][0])
        else:
            log.debug("Could not parse I in normalise json.")
            i_value = float(0)

        lra_matches = re.findall(r'"input_lra" : "(-?([0-9]*\.[0-9]+))",', output)
        if lra_matches:
            # log.debug("lra_matches=%s", lra_matches[0][0])
            lra_value = float(lra_matches[0][0])
        else:
            log.debug("Could not parse LRA in normalise json.")
            lra_value = float(0)

        tp_matches = re.findall(r'"input_tp" : "(-?([0-9]*\.[0-9]+))",', output)
        if tp_matches:
            # log.debug("tp_matches=%s", tp_matches[0][0])
            tp_value = float(tp_matches[0][0])
        else:
            log.debug("Could not parse TP in normalise json.")
            tp_value = float(0)

        thresh_matches = re.findall(r'"input_thresh" : "(-?([0-9]*\.[0-9]+))",', output)
        if thresh_matches:
            # log.debug("thresh_matches=%s", thresh_matches[0][0])
            thresh = float(thresh_matches[0][0])
        else:
            log.debug("Could not parse thresh in normalise json.")
            thresh = float(0)

        offset_matches = re.findall(r'"target_offset" : "(-?([0-9]*\.[0-9]+))', output)
        if offset_matches:
            # log.debug("offset_matches=%s", offset_matches[0][0])
            offset = float(offset_matches[0][0])
        else:
            log.debug("Could not parse offset in normalise json.")
            offset = float(0)

        loudnorm_opts = (
            "-af loudnorm=I=-24.0:LRA=7.0:TP=-2.0:linear=true:"
            f"measured_I={i_value}:"
            f"measured_LRA={lra_value}:"
            f"measured_TP={tp_value}:"
            f"measured_thresh={thresh}:"
            f"offset={offset}"
        )
        return loudnorm_opts

    async def get_loudnorm_gain(self, input_file: str) -> float:
        """
        Extract loudnorm offset in dB and convert to linear gain multiplier.
        Returns the gain multiplier (1.0 = no change).
        """
        log.debug("Calculating loudnorm gain for: %s", input_file)

        # Get the loudnorm options (which already extracts offset)
        loudnorm_opts = await self.get_mean_volume(input_file)

        # Extract offset from loudnorm_opts string
        offset_match = re.search(r"offset=([\-]?\d+\.?\d*)", loudnorm_opts)
        if offset_match:
            offset_db = float(offset_match.group(1))
            # Convert dB to linear gain: gain = 10^(offset/20)
            gain = 10 ** (offset_db / 20)
            log.debug("Loudnorm offset: %.2f dB, gain: %.4f", offset_db, gain)
            return gain
        else:
            log.warning("Could not extract loudnorm offset, using 1.0 (no gain)")
            return 1.0

    async def _really_download(self) -> None:
        """
        Actually download the media in this entry into cache.
        """
        log.info("Download started:  %r", self)

        info = None
        for attempt in range(1, 4):
            log.everything(  # type: ignore[attr-defined]
                "Download attempt %s of 3...", attempt
            )
            try:
                info = await self.downloader.extract_info(self.url, download=True)
                break
            except ContentTooShortError as e:
                # this typically means connection was interrupted, any
                # download is probably partial. we should definitely do
                # something about it to prevent broken cached files.
                if attempt < 3:
                    wait_for = 1.5 * attempt
                    log.warning(
                        "Download incomplete, retrying in %.1f seconds.  Reason: %s",
                        wait_for,
                        str(e),
                    )
                    await asyncio.sleep(wait_for)  # TODO: backoff timer maybe?
                    continue

                # Mark the file I guess, and maintain the default of raising ExtractionError.
                log.error("Download failed, not retrying! Reason:  %s", str(e))
                self.cache_busted = True
                raise ExtractionError(str(e)) from e
            except YoutubeDLError as e:
                # as a base exception for any exceptions raised by yt_dlp.
                raise ExtractionError(str(e)) from e

            except Exception as e:
                log.error("Extraction encountered an unhandled exception.")
                raise MusicbotException(str(e)) from e

        if info is None:
            log.error("Download failed:  %r", self)
            raise ExtractionError("Failed to extract data for the requested media.")

        log.info("Download complete:  %r", self)

        self._is_downloaded = True
        self.filename = info.expected_filename or ""
        # Strip .json extension if present (yt-dlp may include it for metadata files)
        if self.filename.endswith(".json"):
            self.filename = self.filename[:-5]

        # It should be safe to get our newly downloaded file size now...
        # This should also leave self.downloaded_bytes set to 0 if the file is in cache already.
        self.downloaded_bytes = os.path.getsize(self.filename)

        # Load audio into memory if config is enabled and memory capacity available
        if self.playlist.bot.config.load_audio_into_memory:
            # Estimate PCM16 size: compressed audio is typically ~10x smaller
            estimated_pcm_size = self.downloaded_bytes * 10
            if not self.playlist.bot.filecache.has_memory_capacity(estimated_pcm_size):
                log.info(
                    "Memory limit reached, skipping in-memory load for: %s",
                    self.title,
                )
            else:
                try:
                    log.debug("Loading audio into memory: %s", self.title)
                    # Decode to PCM16, applying loudnorm EQ if enabled
                    loudnorm_gain = getattr(self, "_loudnorm_gain", 1.0)
                    pcm_data = await decode_to_pcm16(self.filename, loudnorm_gain)
                    if pcm_data:
                        self.memory_data = pcm_data
                        self.memory_size = len(pcm_data)

                        if self.playlist.bot.filecache.allocate_memory(
                            self.memory_size
                        ):
                            log.debug(
                                "Successfully loaded into memory: %d bytes PCM16",
                                self.memory_size,
                            )
                        else:
                            # Release the memory we tried to allocate
                            self.memory_data = None
                            self.memory_size = 0
                            log.debug(
                                "Memory allocation failed after decoding: %d bytes PCM16",
                                estimated_pcm_size,
                            )
                    else:
                        log.error("Failed to decode audio to PCM16: %s", self.title)
                        self.memory_data = None
                        self.memory_size = 0
                except Exception as e:
                    log.error(
                        "Failed to load audio into memory: %s, error: %s",
                        self.title,
                        str(e),
                    )
                    self.memory_data = None
                    self.memory_size = 0


class StreamPlaylistEntry(BasePlaylistEntry):
    SERIAL_VERSION: int = 3

    def __init__(
        self,
        playlist: "Playlist",
        info: YtdlpResponseDict,
        author: Optional["discord.Member"] = None,
        channel: Optional[GuildMessageableChannels] = None,
    ) -> None:
        """
        Create Stream Playlist entry that will be sent directly to ffmpeg for playback.

        :param: playlist:  The playlist object this entry should belong to.
        :param: info:  A YtdlResponseDict with from downloader.extract_info()
        :param: from_apl:  Flag this entry as automatic playback, not queued by a user.
        :param: meta:  a collection extra of key-values stored with the entry.
        """
        super().__init__()

        self.playlist: "Playlist" = playlist
        self.info: YtdlpResponseDict = info

        self.author: Optional["discord.Member"] = author
        self.channel: Optional[GuildMessageableChannels] = channel

        self.filename: str = self.url

    @property
    def from_auto_playlist(self) -> bool:
        """Returns true if the entry has an author or a channel."""
        if self.author is not None or self.channel is not None:
            return False
        return True

    @property
    def url(self) -> str:
        """get extracted url if available or otherwise return the input subject"""
        if self.info.extractor and self.info.url:
            return self.info.url
        return self.info.input_subject

    @property
    def title(self) -> str:
        """Gets a title string from entry info or 'Unknown'"""
        # special case for twitch streams, from previous code.
        # TODO: test coverage here
        if self.info.extractor == "twitch:stream":
            dtitle = self.info.get("description", None)
            if dtitle and not self.info.title:
                return str(dtitle)

        # TODO: i18n for this at some point.
        return self.info.title or "Unknown"

    @property
    def duration(self) -> Optional[float]:
        """Gets available duration data or None"""
        # duration can be 0, if so we make sure it returns None instead.
        return self.info.get("duration", None) or None

    @duration.setter
    def duration(self, value: float) -> None:
        self.info["duration"] = value

    @property
    def duration_td(self) -> datetime.timedelta:
        """
        Get timedelta object from any known duration data.
        May contain a 0 second duration.
        """
        t = self.duration or 0
        return datetime.timedelta(seconds=t)

    @property
    def thumbnail_url(self) -> str:
        """Get available thumbnail from info or an empty string"""
        return self.info.thumbnail_url

    @property
    def playback_speed(self) -> float:
        """Playback speed for streamed entries cannot typically be adjusted."""
        return 1.0

    def __json__(self) -> Dict[str, Any]:
        return self._enclose_json(
            {
                "version": StreamPlaylistEntry.SERIAL_VERSION,
                "info": self.info.data,
                "filename": self.filename,
                "author_id": self.author.id if self.author else None,
                "channel_id": self.channel.id if self.channel else None,
            }
        )

    @classmethod
    def _deserialize(
        cls,
        raw_json: Dict[str, Any],
        playlist: Optional["Playlist"] = None,
        **kwargs: Any,
    ) -> Optional["StreamPlaylistEntry"]:
        assert playlist is not None, cls._bad("playlist")

        vernum = raw_json.get("version", None)
        if not vernum:
            log.error("Entry data is missing version number, cannot deserialize.")
            return None
        if vernum != URLPlaylistEntry.SERIAL_VERSION:
            log.error("Entry data has the wrong version number, cannot deserialize.")
            return None

        try:
            info = YtdlpResponseDict(raw_json["info"])
            filename = raw_json["filename"]

            channel_id = raw_json.get("channel_id", None)
            if channel_id:
                o_channel = playlist.bot.get_channel(int(channel_id))

                if not o_channel:
                    log.warning(
                        "Deserialized StreamPlaylistEntry cannot find channel with id:  %s",
                        raw_json["channel_id"],
                    )

                if isinstance(
                    o_channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    channel = o_channel
                else:
                    log.warning(
                        "Deserialized StreamPlaylistEntry has the wrong channel type:  %s",
                        type(o_channel),
                    )
                    channel = None
            else:
                channel = None

            author_id = raw_json.get("author_id", None)
            if author_id:
                if isinstance(
                    channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    author = channel.guild.get_member(int(author_id))

                    if not author:
                        log.warning(
                            "Deserialized StreamPlaylistEntry cannot find author with id:  %s",
                            raw_json["author_id"],
                        )
                else:
                    author = None
                    log.warning(
                        "Deserialized StreamPlaylistEntry has an author ID but no channel for lookup!",
                    )
            else:
                author = None

            entry = cls(playlist, info, author=author, channel=channel)
            entry.filename = filename
            # Stream entries don't load into memory - they're actual streams
            entry.memory_data = None
            entry.memory_size = 0
            return entry
        except (ValueError, KeyError, TypeError) as e:
            log.error("Could not load %s", cls.__name__, exc_info=e)

        return None

    async def _download(self) -> None:
        log.debug("Getting ready for entry:  %r", self)
        self._is_downloading = True
        self._is_downloaded = True
        self.filename = self.url
        self._is_downloading = False

        self._for_each_future(lambda future: future.set_result(self))


class LocalFilePlaylistEntry(BasePlaylistEntry):
    SERIAL_VERSION: int = 1

    def __init__(
        self,
        playlist: "Playlist",
        info: YtdlpResponseDict,
        author: Optional["discord.Member"] = None,
        channel: Optional[GuildMessageableChannels] = None,
    ) -> None:
        """
        Create URL Playlist entry that will be downloaded for playback.

        :param: playlist:  The playlist object this entry should belong to.
        :param: info:  A YtdlResponseDict from downloader.extract_info()
        """
        super().__init__()

        self._start_time: Optional[float] = None
        self._playback_rate: Optional[float] = None
        self._loudnorm_gain: float = 1.0  # Gain multiplier from loudnorm EQ
        self.playlist: "Playlist" = playlist

        self.info: YtdlpResponseDict = info
        self.filename = self.expected_filename or ""
        # Strip .json extension if present (yt-dlp may include it for metadata files)
        if self.filename.endswith(".json"):
            self.filename = self.filename[:-5]

        # TODO: maybe it is worth getting duration as early as possible...

        self.author: Optional["discord.Member"] = author
        self.channel: Optional[GuildMessageableChannels] = channel

        self._aopt_eq: str = ""

    @property
    def aoptions(self) -> str:
        """After input options for ffmpeg to use with this entry."""
        aopts = f"{self._aopt_eq}"
        # Set playback speed options if needed.
        if self._playback_rate is not None or self.playback_speed != 1.0:
            # Append to the EQ options if they are set.
            if self._aopt_eq:
                aopts = f"{self._aopt_eq},atempo={self.playback_speed:.3f}"
            else:
                aopts = f"-af atempo={self.playback_speed:.3f}"

        if aopts:
            return f"{aopts} -vn"

        return "-vn"

    @property
    def boptions(self) -> str:
        """Before input options for ffmpeg to use with this entry."""
        if self._start_time is not None:
            return f"-ss {self._start_time}"
        return ""

    @property
    def from_auto_playlist(self) -> bool:
        """Returns true if the entry has an author or a channel."""
        if self.author is not None or self.channel is not None:
            return False
        return True

    @property
    def url(self) -> str:
        """Gets a playable URL from this entries info."""
        return self.info.get_playable_url()

    @property
    def title(self) -> str:
        """Gets a title string from entry info or 'Unknown'"""
        # TODO: i18n for this at some point.
        return self.info.title or "Unknown"

    @property
    def duration(self) -> Optional[float]:
        """Gets available duration data or None"""
        # duration can be 0, if so we make sure it returns None instead.
        return self.info.get("duration", None) or None

    @duration.setter
    def duration(self, value: float) -> None:
        self.info["duration"] = value

    @property
    def duration_td(self) -> datetime.timedelta:
        """
        Returns duration as a datetime.timedelta object.
        May contain 0 seconds duration.
        """
        t = self.duration or 0
        return datetime.timedelta(seconds=t)

    @property
    def thumbnail_url(self) -> str:
        """Get available thumbnail from info or an empty string"""
        return self.info.thumbnail_url

    @property
    def expected_filename(self) -> Optional[str]:
        """Get the expected filename from info if available or None"""
        return self.info.get("__expected_filename", None)

    def __json__(self) -> Dict[str, Any]:
        """
        Handles representing this object as JSON.
        """
        # WARNING:  if you change data or keys here, you must increase SERIAL_VERSION.
        return self._enclose_json(
            {
                "version": LocalFilePlaylistEntry.SERIAL_VERSION,
                "info": self.info.data,
                "filename": self.filename,
                "author_id": self.author.id if self.author else None,
                "channel_id": self.channel.id if self.channel else None,
                "aoptions": self.aoptions,
            }
        )

    @classmethod
    def _deserialize(
        cls,
        raw_json: Dict[str, Any],
        playlist: Optional["Playlist"] = None,
        **kwargs: Dict[str, Any],
    ) -> Optional["LocalFilePlaylistEntry"]:
        """
        Handles converting from JSON to LocalFilePlaylistEntry.
        """
        # WARNING:  if you change data or keys here, you must increase SERIAL_VERSION.

        # yes this is an Optional that is, in fact, not Optional. :)
        assert playlist is not None, cls._bad("playlist")

        vernum: Optional[int] = raw_json.get("version", None)
        if not vernum:
            log.error("Entry data is missing version number, cannot deserialize.")
            return None
        if vernum != LocalFilePlaylistEntry.SERIAL_VERSION:
            log.error("Entry data has the wrong version number, cannot deserialize.")
            return None

        try:
            info = YtdlpResponseDict(raw_json["info"])
            downloaded = (
                raw_json["downloaded"] if playlist.bot.config.save_videos else False
            )
            filename = raw_json["filename"] if downloaded else None

            channel_id = raw_json.get("channel_id", None)
            if channel_id:
                o_channel = playlist.bot.get_channel(int(channel_id))

                if not o_channel:
                    log.warning(
                        "Deserialized LocalFilePlaylistEntry cannot find channel with id:  %s",
                        raw_json["channel_id"],
                    )

                if isinstance(
                    o_channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    channel = o_channel
                else:
                    log.warning(
                        "Deserialized LocalFilePlaylistEntry has the wrong channel type:  %s",
                        type(o_channel),
                    )
                    channel = None
            else:
                channel = None

            author_id = raw_json.get("author_id", None)
            if author_id:
                if isinstance(
                    channel,
                    (
                        discord.Thread,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                    ),
                ):
                    author = channel.guild.get_member(int(author_id))

                    if not author:
                        log.warning(
                            "Deserialized LocalFilePlaylistEntry cannot find author with id:  %s",
                            raw_json["author_id"],
                        )
                else:
                    author = None
                    log.warning(
                        "Deserialized LocalFilePlaylistEntry has an author ID but no channel for lookup!",
                    )
            else:
                author = None

            entry = cls(playlist, info, author=author, channel=channel)
            entry.filename = filename

            return entry
        except (ValueError, TypeError, KeyError) as e:
            log.error("Could not load %s", cls.__name__, exc_info=e)

        return None

    @property
    def start_time(self) -> float:
        if self._start_time is not None:
            return self._start_time
        return 0

    def set_start_time(self, start_time: float) -> None:
        """Sets a start time in seconds to use with the ffmpeg -ss flag."""
        self._start_time = start_time

    @property
    def playback_speed(self) -> float:
        """Get the current playback speed if one was set, or return 1.0 for normal playback."""
        if self._playback_rate is not None:
            return self._playback_rate
        return self.playlist.bot.config.default_speed or 1.0

    def set_playback_speed(self, speed: float) -> None:
        """Set the playback speed to be used with ffmpeg -af:atempo filter."""
        self._playback_rate = speed

    async def _download(self) -> None:
        """
        Handle readying the local media file, by extracting info like duration
        or setting up normalized audio options.
        """
        if self._is_downloading:
            return

        log.debug("Getting ready for entry:  %r", self)

        self._is_downloading = True
        try:
            # check for duration and attempt to extract it if missing.
            if self.duration is None:
                # optional pymediainfo over ffprobe?
                if pymediainfo:
                    self.duration = self._get_duration_pymedia(self.filename)

                # no matter what, ffprobe should be available.
                if self.duration is None:
                    self.duration = await self._get_duration_ffprobe(self.filename)

                if not self.duration:
                    log.error(
                        "MusicBot could not get duration data for this entry.\n"
                        "Queue time estimation may be unavailable until this track is cleared.\n"
                        "Entry file: %s",
                        self.filename,
                    )
                else:
                    log.debug(
                        "Got duration of %s seconds for file:  %s",
                        self.duration,
                        self.filename,
                    )

            if self.playlist.bot.config.use_experimental_equalization:
                try:
                    self._aopt_eq = await self.get_mean_volume(self.filename)
                    if self._aopt_eq:
                        self._loudnorm_gain = await self.get_loudnorm_gain(
                            self.filename
                        )

                # Unfortunate evil that we abide for now...
                except Exception:  # pylint: disable=broad-exception-caught
                    log.error(
                        "There as a problem with working out EQ, likely caused by a strange installation of FFmpeg. "
                        "This has not impacted the ability for the bot to work, but will mean your tracks will not be equalised.",
                        exc_info=True,
                    )

            # Trigger ready callbacks.
            self._is_downloaded = True

            # Load local file into memory if config is enabled
            if self.playlist.bot.config.load_audio_into_memory and not self.memory_data:
                # Estimate PCM16 size: compressed audio is typically ~10x smaller
                estimated_pcm_size = os.path.getsize(self.filename) * 10
                if not self.playlist.bot.filecache.has_memory_capacity(
                    estimated_pcm_size
                ):
                    log.debug(
                        "Memory limit reached, skipping in-memory load for local file: %s",
                        self.title,
                    )
                else:
                    try:
                        log.debug("Loading local file into memory: %s", self.title)
                        # Decode to PCM16, applying loudnorm EQ if enabled
                        loudnorm_gain = getattr(self, "_loudnorm_gain", 1.0)
                        pcm_data = await decode_to_pcm16(self.filename, loudnorm_gain)
                        if pcm_data:
                            self.memory_data = pcm_data
                            self.memory_size = len(pcm_data)

                            if self.playlist.bot.filecache.allocate_memory(
                                self.memory_size
                            ):
                                log.debug(
                                    "Successfully loaded local file into memory: %d bytes PCM16",
                                    self.memory_size,
                                )
                            else:
                                self.memory_data = None
                                self.memory_size = 0
                        else:
                            log.error(
                                "Failed to decode local file to PCM16: %s",
                                self.title,
                            )
                            self.memory_data = None
                            self.memory_size = 0
                    except Exception as e:
                        log.error(
                            "Failed to load local file into memory: %s, error: %s",
                            self.title,
                            str(e),
                        )
                        self.memory_data = None
                        self.memory_size = 0

            self._for_each_future(lambda future: future.set_result(self))

        # Flake8 thinks 'e' is never used, and later undefined. Maybe the lambda is too much.
        except Exception as e:  # pylint: disable=broad-exception-caught
            ex = e
            if log.getEffectiveLevel() <= logging.DEBUG:
                log.error("Exception while checking entry data.")
            self._for_each_future(lambda future: future.set_exception(ex))

        finally:
            self._is_downloading = False

    def _get_duration_pymedia(self, input_file: str) -> Optional[float]:
        """
        Tries to use pymediainfo module to extract duration, if the module is available.
        """
        if pymediainfo:
            log.debug("Trying to get duration via pymediainfo for:  %s", input_file)
            try:
                mediainfo = pymediainfo.MediaInfo.parse(input_file)
                if mediainfo.tracks:
                    return int(mediainfo.tracks[0].duration) / 1000
            except (FileNotFoundError, OSError, RuntimeError, ValueError, TypeError):
                log.exception("Failed to get duration via pymediainfo.")
        return None

    async def _get_duration_ffprobe(self, input_file: str) -> Optional[float]:
        """
        Tries to use ffprobe to extract duration from media if possible.
        """
        log.debug("Trying to get duration via ffprobe for:  %s", input_file)
        ffprobe_bin = shutil.which("ffprobe")
        if not ffprobe_bin:
            log.error("Could not locate ffprobe in your path!")
            return None

        ffprobe_cmd = [
            ffprobe_bin,
            "-i",
            self.filename,
            "-show_entries",
            "format=duration",
            "-v",
            "quiet",
            "-of",
            "csv=p=0",
        ]

        try:
            raw_output = await run_command(ffprobe_cmd)
            output = raw_output.decode("utf8")
            return float(output)
        except (ValueError, UnicodeError):
            log.error(
                "ffprobe returned something that could not be used.", exc_info=True
            )
        except Exception:  # pylint: disable=broad-exception-caught
            log.exception("ffprobe could not be executed for some reason.")

        return None

    async def get_mean_volume(self, input_file: str) -> str:
        """
        Attempt to calculate the mean volume of the `input_file` by using
        output from ffmpeg to provide values which can be used by command
        arguments sent to ffmpeg during playback.
        """
        log.debug("Calculating mean volume of:  %s", input_file)
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            log.error("Could not locate ffmpeg on your path!")
            return ""

        # NOTE: this command should contain JSON, but I have no idea how to make
        # ffmpeg spit out only the JSON.
        ffmpeg_cmd = [
            ffmpeg_bin,
            "-i",
            input_file,
            "-af",
            "loudnorm=I=-24.0:LRA=7.0:TP=-2.0:linear=true:print_format=json",
            "-f",
            "null",
            "/dev/null",
            "-hide_banner",
            "-nostats",
        ]

        raw_output = await run_command(ffmpeg_cmd)
        output = raw_output.decode("utf-8")

        i_matches = re.findall(r'"input_i" : "(-?([0-9]*\.[0-9]+))",', output)
        if i_matches:
            # log.debug("i_matches=%s", i_matches[0][0])
            i_value = float(i_matches[0][0])
        else:
            log.debug("Could not parse I in normalise json.")
            i_value = float(0)

        lra_matches = re.findall(r'"input_lra" : "(-?([0-9]*\.[0-9]+))",', output)
        if lra_matches:
            # log.debug("lra_matches=%s", lra_matches[0][0])
            lra_value = float(lra_matches[0][0])
        else:
            log.debug("Could not parse LRA in normalise json.")
            lra_value = float(0)

        tp_matches = re.findall(r'"input_tp" : "(-?([0-9]*\.[0-9]+))",', output)
        if tp_matches:
            # log.debug("tp_matches=%s", tp_matches[0][0])
            tp_value = float(tp_matches[0][0])
        else:
            log.debug("Could not parse TP in normalise json.")
            tp_value = float(0)

        thresh_matches = re.findall(r'"input_thresh" : "(-?([0-9]*\.[0-9]+))",', output)
        if thresh_matches:
            # log.debug("thresh_matches=%s", thresh_matches[0][0])
            thresh = float(thresh_matches[0][0])
        else:
            log.debug("Could not parse thresh in normalise json.")
            thresh = float(0)

        offset_matches = re.findall(r'"target_offset" : "(-?([0-9]*\.[0-9]+))', output)
        if offset_matches:
            # log.debug("offset_matches=%s", offset_matches[0][0])
            offset = float(offset_matches[0][0])
        else:
            log.debug("Could not parse offset in normalise json.")
            offset = float(0)

        loudnorm_opts = (
            "-af loudnorm=I=-24.0:LRA=7.0:TP=-2.0:linear=true:"
            f"measured_I={i_value}:"
            f"measured_LRA={lra_value}:"
            f"measured_TP={tp_value}:"
            f"measured_thresh={thresh}:"
            f"offset={offset}"
        )
        return loudnorm_opts
