from __future__ import annotations

import hashlib
import json
import re
import sys
import warnings
from collections.abc import Generator
from typing import Any, Union

import click
from bs4 import XMLParsedAsHTMLWarning
from click import Context
from devine.core.manifests import DASH, HLS
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series
from devine.core.tracks import Audio, Chapter, Subtitle, Track, Tracks, Video
from devine.core.utils.collections import as_list
from devine.core.utils.sslciphers import SSLCiphers

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


class iP(Service):
    """
    \b
    Service code for the BBC iPlayer streaming service (https://www.bbc.co.uk/iplayer).
    Base code from VT, credit to original author

    \b
    Author: stabbedbybrick
    Authorization: None
    Security: None

    \b
    Tips:
        - Use full title URL as input for best results.
    \b
        - An SSL certificate (PEM) is required for accessing the UHD endpoint.
        Specify its path using the service configuration data in the root config:
    \b
            services:
                iP:
                    cert: path/to/cert
    \b
        - Use -v H.265 to request UHD tracks
    """

    ALIASES = ("bbciplayer", "bbc", "iplayer")
    GEOFENCE = ("gb",)
    TITLE_RE = r"^(?:https?://(?:www\.)?bbc\.co\.uk/(?:iplayer/(?P<kind>episode|episodes)/|programmes/))?(?P<id>[a-z0-9]+)(?:/.*)?$"

    @staticmethod
    @click.command(name="iP", short_help="https://www.bbc.co.uk/iplayer", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> iP:
        return iP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        self.vcodec = ctx.parent.params.get("vcodec")
        super().__init__(ctx)

        if self.vcodec == "H.265" and not self.config.get("cert"):
            self.log.error("H.265 cannot be selected without a certificate")
            sys.exit(1)

        quality = ctx.parent.params.get("quality")
        if quality and quality[0] > 1080 and self.vcodec != "H.265" and self.config.get("cert"):
            self.log.info(" + Switched video codec to H.265 to be able to get 2160p video track")
            self.vcodec = "H.265"

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "q": self.title,
            "apikey": self.config["api_key"],
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()

        results = r.json()
        for result in results["results"]:
            yield SearchResult(
                id_=result.get("uri").split(":")[-1],
                title=result.get("title"),
                description=result.get("synopsis"),
                label="series" if result.get("type", "") == "brand" else result.get("type"),
                url=result.get("url"),
            )

    def get_titles(self) -> Union[Movies, Series]:
        kind, pid = (re.match(self.TITLE_RE, self.title).group(i) for i in ("kind", "id"))
        if not pid:
            self.log.error("Unable to parse title ID - is the URL or id correct?")
            sys.exit(1)

        data = self.get_data(pid, slice_id=None)
        if data is None and kind == "episode":
            return self.get_single_episode(self.title)
        elif data is None:
            self.log.error("Metadata was not found - if %s is an episode, use full URL as input", pid)
            sys.exit(1)

        if "Film" in data["labels"]["category"]:
            return Movies(
                [
                    Movie(
                        id_=data["id"],
                        name=data["title"]["default"],
                        year=None,  # TODO
                        service=self.__class__,
                        language="en",
                        data=data,
                    )
                ]
            )
        else:
            seasons = [self.get_data(pid, x["id"]) for x in data["slices"] or [{"id": None}]]
            episodes = [self.create_episode(episode) for season in seasons for episode in season["entities"]["results"]]
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        playlist = self.session.get(url=self.config["endpoints"]["playlist"].format(pid=title.id)).json()
        if not playlist["defaultAvailableVersion"]:
            self.log.error(" - Title is unavailable")
            sys.exit(1)

        if self.config.get("cert"):
            url = self.config["endpoints"]["manifest_"].format(
                vpid=playlist["defaultAvailableVersion"]["smpConfig"]["items"][0]["vpid"],
                mediaset="iptv-uhd" if self.vcodec == "H.265" else "iptv-all",
            )

            session = self.session
            session.mount("https://", SSLCiphers())
            session.mount("http://", SSLCiphers())
            manifest = session.get(
                url, headers={"user-agent": self.config["user_agent"]}, cert=self.config["cert"]
            ).json()

            if "result" in manifest:
                self.log.error(f" - Failed to get manifest [{manifest['result']}]")
                sys.exit(1)

        else:
            url = self.config["endpoints"]["manifest"].format(
                vpid=playlist["defaultAvailableVersion"]["smpConfig"]["items"][0]["vpid"],
                mediaset="iptv-all",
            )
            manifest = self.session.get(url).json()

        connection = {}
        for video in [x for x in manifest["media"] if x["kind"] == "video"]:
            connections = sorted(video["connection"], key=lambda x: x["priority"])
            if self.vcodec == "H.265":
                connection = connections[0]
            else:
                connection = next(
                    x for x in connections if x["supplier"] == "mf_akamai" and x["transferFormat"] == "dash"
                )

            break

        if not self.vcodec == "H.265":
            if connection["transferFormat"] == "dash":
                connection["href"] = "/".join(
                    connection["href"].replace("dash", "hls").split("?")[0].split("/")[0:-1] + ["hls", "master.m3u8"]
                )
                connection["transferFormat"] = "hls"
            elif connection["transferFormat"] == "hls":
                connection["href"] = "/".join(
                    connection["href"].replace(".hlsv2.ism", "").split("?")[0].split("/")[0:-1] + ["hls", "master.m3u8"]
                )

            if connection["transferFormat"] != "hls":
                raise ValueError(f"Unsupported video media transfer format {connection['transferFormat']!r}")

        if connection["transferFormat"] == "dash":
            tracks = DASH.from_url(url=connection["href"], session=self.session).to_tracks(language=title.language)
        elif connection["transferFormat"] == "hls":
            tracks = HLS.from_url(url=connection["href"], session=self.session).to_tracks(language=title.language)
        else:
            raise ValueError(f"Unsupported video media transfer format {connection['transferFormat']!r}")

        for video in tracks.videos:
            # TODO: add HLG to UHD tracks

            if any(re.search(r"-audio_\w+=\d+", x) for x in as_list(video.url)):
                # create audio stream from the video stream
                audio_url = re.sub(r"-video=\d+", "", as_list(video.url)[0])
                audio = Audio(
                    # use audio_url not video url, as to ignore video bitrate in ID
                    id_=hashlib.md5(audio_url.encode()).hexdigest()[0:7],
                    url=audio_url,
                    codec=Audio.Codec.from_codecs("mp4a"),
                    language=[v.language for v in video.data["hls"]["playlist"].media][0],
                    bitrate=int(self.find(r"-audio_\w+=(\d+)", as_list(video.url)[0]) or 0),
                    channels=[v.channels for v in video.data["hls"]["playlist"].media][0],
                    descriptive=False,  # Not available
                    descriptor=Track.Descriptor.HLS,
                )
                if not tracks.exists(by_id=audio.id):
                    # some video streams use the same audio, so natural dupes exist
                    tracks.add(audio)
                # remove audio from the video stream
                video.url = [re.sub(r"-audio_\w+=\d+", "", x) for x in as_list(video.url)][0]
                video.codec = Video.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs)
                video.bitrate = int(self.find(r"-video=(\d+)", as_list(video.url)[0]) or 0)

        for caption in [x for x in manifest["media"] if x["kind"] == "captions"]:
            connection = sorted(caption["connection"], key=lambda x: x["priority"])[0]
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(connection["href"].encode()).hexdigest()[0:6],
                    url=connection["href"],
                    codec=Subtitle.Codec.from_codecs("ttml"),
                    language=title.language,
                    is_original_lang=True,
                    forced=False,
                    sdh=True,
                )
            )
            break

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> list[Chapter]:
        return []

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        return None
    
    # service specific functions

    def get_data(self, pid: str, slice_id: str) -> dict:
        json_data = {
            "id": "9fd1636abe711717c2baf00cebb668de",
            "variables": {
                "id": pid,
                "perPage": 200,
                "page": 1,
                "sliceId": slice_id if slice_id else None,
            },
        }

        r = self.session.post(self.config["endpoints"]["metadata"], json=json_data)
        r.raise_for_status()

        return r.json()["data"]["programme"]

    def create_episode(self, episode):
        title = episode["episode"]["title"]["default"].strip()
        subtitle = episode["episode"]["subtitle"]
        series = re.finditer(r"Series (\d+):|Season (\d+):|(\d{4}/\d{2}): Episode \d+", subtitle.get("default"))
        season_num = int(next((m.group(1) or m.group(2) or m.group(3).replace("/", "") for m in series), 0))

        number = re.finditer(r"(\d+)\.|Episode (\d+)", subtitle.get("slice") or subtitle.get("default") or "")
        ep_num = int(next((m.group(1) or m.group(2) for m in number), 0))

        name = re.search(r"\d+\. (.+)", subtitle.get("slice") or "")
        ep_name = name.group(1) if name else subtitle.get("slice") or ""
        if not subtitle.get("slice"):
            ep_name = subtitle.get("default") or ""

        return Episode(
            id_=episode["episode"].get("id"),
            service=self.__class__,
            title=title,
            season=season_num,
            number=ep_num,
            name=ep_name,
            language="en",
        )
    
    def get_single_episode(self, url: str) -> Series:
        r = self.session.get(url)
        r.raise_for_status()

        redux = re.search(
            "window.__IPLAYER_REDUX_STATE__ = (.*?);</script>", r.text
        ).group(1)
        data = json.loads(redux)
        subtitle = data["episode"].get("subtitle")

        if subtitle is not None:
            season_match = re.search(r"Series (\d+):", subtitle)
            season = int(season_match.group(1)) if season_match else 0
            number_match = re.finditer(r"(\d+)\.|Episode (\d+)", subtitle)
            number = int(next((m.group(1) or m.group(2) for m in number_match), 0))
            name_match = re.search(r"\d+\. (.+)", subtitle)
            name = (
                name_match.group(1)
                if name_match
                else subtitle
                if not re.search(r"Series (\d+): Episode (\d+)", subtitle)
                else ""
            )

        return Series(
            [
                Episode(
                    id_=data["episode"]["id"],
                    service=self.__class__,
                    title=data["episode"]["title"],
                    season=season if subtitle else 0,
                    number=number if subtitle else 0,
                    name=name if subtitle else "",
                    language="en",
                )
            ]
        )

    def find(self, pattern, string, group=None):
        if group:
            m = re.search(pattern, string)
            if m:
                return m.group(group)
        else:
            return next(iter(re.findall(pattern, string)), None)
