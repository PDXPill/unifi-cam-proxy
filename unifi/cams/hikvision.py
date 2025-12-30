import argparse
import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx
import xmltodict
from hikvisionapi import AsyncClient

from unifi.cams.base import UnifiCamBase


class HikvisionCam(UnifiCamBase):
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        super().__init__(args, logger)
        self.snapshot_dir = tempfile.mkdtemp()
        self.streams = {}
        self.cam = AsyncClient(
            f"http://{self.args.ip}",
            self.args.username,
            self.args.password,
            timeout=None,
        )
        self.channel = args.channel
        self.substream = args.substream
        self.ptz_supported = False
        self.motion_in_progress: bool = False
        self._last_event_timestamp: Union[str, int] = 0
        self._stream_meta: dict[int, dict[str, int]] = {}

    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        super().add_parser(parser)
        parser.add_argument("--username", "-u", required=True, help="Camera username")
        parser.add_argument("--password", "-p", required=True, help="Camera password")
        parser.add_argument(
            "--channel", "-c", default=1, type=int, help="Camera channel index"
        )
        parser.add_argument(
            "--substream", "-s", default=3, type=int, help="Camera substream index"
        )

    async def get_snapshot(self) -> Path:
        img_file = Path(self.snapshot_dir, "screen.jpg")
        source = int(f"{self.channel}01")
        try:
            with img_file.open("wb") as f:
                async for chunk in self.cam.Streaming.channels[source].picture(
                    method="get", type="opaque_data"
                ):
                    if chunk:
                        f.write(chunk)
        except httpx.RequestError:
            pass
        return img_file

    async def check_ptz_support(self, channel) -> bool:
        try:
            await self.cam.PTZCtrl.channels[channel].capabilities(method="get")
            self.logger.info("Detected PTZ support")
            return True
        except (httpx.RequestError, httpx.HTTPStatusError):
            pass
        return False

    async def get_video_settings(self) -> dict[str, Any]:
        if self.ptz_supported:
            r = (await self.cam.PTZCtrl.channels[1].status(method="get"))["PTZStatus"][
                "AbsoluteHigh"
            ]
            return {
                # Tilt/elevation
                "brightness": int(100 * int(r["azimuth"]) / 3600),
                # Pan/azimuth
                "contrast": int(100 * int(r["azimuth"]) / 3600),
                # Zoom
                "hue": int(100 * int(r["absoluteZoom"]) / 40),
            }
        return {}

    async def change_video_settings(self, options: dict[str, Any]) -> None:
        if self.ptz_supported:
            tilt = int((900 * int(options["brightness"])) / 100)
            pan = int((3600 * int(options["contrast"])) / 100)
            zoom = int((40 * int(options["hue"])) / 100)

            self.logger.info("Moving to %s:%s:%s", pan, tilt, zoom)
            req = {
                "PTZData": {
                    "@version": "2.0",
                    "@xmlns": "http://www.hikvision.com/ver20/XMLSchema",
                    "AbsoluteHigh": {
                        "absoluteZoom": str(zoom),
                        "azimuth": str(pan),
                        "elevation": str(tilt),
                    },
                }
            }
            await self.cam.PTZCtrl.channels[1].absolute(
                method="put", data=xmltodict.unparse(req, pretty=True)
            )

    async def _fetch_stream_meta(self, stream_id: int) -> Optional[dict[str, int]]:
        try:
            resp = await self.cam.Streaming.channels[stream_id](method="get")
        except httpx.RequestError as exc:
            self.logger.warning(
                "Failed to fetch stream settings for %s: %s", stream_id, exc
            )
            return None

        def _to_int(value) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        try:
            stream = resp.get("StreamingChannel", {})
            video = stream.get("Video", {})
            width = _to_int(
                video.get("videoResolutionWidth") or video.get("width") or 0
            )
            height = _to_int(
                video.get("videoResolutionHeight") or video.get("height") or 0
            )
            fps = _to_int(video.get("maxFrameRate") or video.get("videoFrameRate"))
            bitrate_kbps = _to_int(
                video.get("maxBitRate")
                or video.get("videoBitrate")
                or video.get("constantBitRate")
            )
            gop = _to_int(video.get("keyFrameInterval") or video.get("GOP"))
        except AttributeError as exc:
            self.logger.warning(
                "Unexpected stream settings format for %s: %s", stream_id, exc
            )
            return None

        meta: dict[str, int] = {}
        if width and height:
            meta["width"] = width
            meta["height"] = height
        if fps:
            meta["fps"] = fps
        if bitrate_kbps:
            meta["bitrate"] = bitrate_kbps * 1000
        if gop:
            meta["gop"] = gop
        return meta

    async def _ensure_stream_meta(self) -> None:
        if self._stream_meta:
            return

        main_stream = int(f"{self.channel}01")
        sub_stream = int(f"{self.channel}0{self.substream}")
        stream_ids = [main_stream, sub_stream]

        results = await asyncio.gather(
            *(self._fetch_stream_meta(stream_id) for stream_id in stream_ids)
        )
        for stream_id, meta in zip(stream_ids, results):
            if meta:
                self._stream_meta[stream_id] = meta

    async def get_video_profiles(
        self, vid_dst: dict[str, list[str]]
    ) -> dict[str, Any]:
        profiles = await super().get_video_profiles(vid_dst)
        await self._ensure_stream_meta()

        main_stream = int(f"{self.channel}01")
        sub_stream = int(f"{self.channel}0{self.substream}")
        mapping = {
            "video1": main_stream,
            "video2": sub_stream,
            "video3": sub_stream,
        }

        for stream_index, stream_id in mapping.items():
            meta = self._stream_meta.get(stream_id)
            profile = profiles.get(stream_index)
            if not meta or not profile:
                continue

            if "width" in meta and "height" in meta:
                profile["width"] = meta["width"]
                profile["height"] = meta["height"]
            if meta.get("fps"):
                profile["fps"] = meta["fps"]
                if "validFpsValues" in profile:
                    fps_set = set(profile["validFpsValues"])
                    fps_set.add(meta["fps"])
                    profile["validFpsValues"] = sorted(fps_set)
            if meta.get("bitrate"):
                profile["bitRateCbrAvg"] = meta["bitrate"]
                profile["bitRateVbrMax"] = max(
                    meta["bitrate"], profile.get("bitRateVbrMax", meta["bitrate"])
                )
                profile["bitRateVbrMin"] = min(
                    profile.get("bitRateVbrMin", meta["bitrate"]), meta["bitrate"]
                )
                if "currentVbrBitrate" in profile:
                    profile["currentVbrBitrate"] = meta["bitrate"]
                if "validBitrateRangeMax" in profile:
                    profile["validBitrateRangeMax"] = max(
                        profile["validBitrateRangeMax"], meta["bitrate"]
                    )
                if "validBitrateRangeMin" in profile:
                    profile["validBitrateRangeMin"] = min(
                        profile["validBitrateRangeMin"], meta["bitrate"]
                    )
            if meta.get("gop"):
                profile["N"] = meta["gop"]

        return profiles

    async def get_stream_source(self, stream_index: str) -> str:
        substream = 1
        if stream_index != "video1":
            substream = self.substream

        return (
            f"rtsp://{self.args.username}:{self.args.password}@{self.args.ip}:554"
            f"/Streaming/Channels/{self.channel}0{substream}/"
        )

    async def maybe_end_motion_event(self, start_time):
        await asyncio.sleep(2)
        if self.motion_in_progress and self._last_event_timestamp == start_time:
            await self.trigger_motion_stop()
            self.motion_in_progress = False

    async def run(self) -> None:
        self.ptz_supported = await self.check_ptz_support(self.channel)
        return

        while True:
            self.logger.info("Connecting to motion events API")
            try:
                async for event in self.cam.Event.notification.alertStream(
                    method="get", type="stream", timeout=None
                ):
                    alert = event.get("EventNotificationAlert")
                    if (
                        alert
                        and alert.get("channelID") == str(self.channel)
                        and alert.get("eventType") == "VMD"
                    ):
                        self._last_event_timestamp = alert.get("dateTime", time.time())

                        if self.motion_in_progress is False:
                            self.motion_in_progress = True
                            await self.trigger_motion_start()

                        # End motion event after 2 seconds of no updates
                        asyncio.ensure_future(
                            self.maybe_end_motion_event(self._last_event_timestamp)
                        )
            except httpx.RequestError:
                self.logger.error("Motion API request failed, retrying")
