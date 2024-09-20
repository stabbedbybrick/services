from __future__ import annotations

import hashlib
import json
import re
import sys
import warnings
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Union

import click
from bs4 import XMLParsedAsHTMLWarning
from click import Context
from devine.core.manifests import DASH, HLS
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series
from devine.core.tracks import Audio, Chapter, Subtitle, Tracks, Video
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
        - Use --list-titles before anything, iPlayer's listings are often messed up.
    \b
        - An SSL certificate (PEM) is required for accessing the UHD endpoint.
        Specify its path using the service configuration data in the root config:
    \b
            services:
                iP:
                    cert: path/to/cert
    \b
        - Use --range HLG to request H.265 UHD tracks
        - See which titles are available in UHD:
            https://www.bbc.co.uk/iplayer/help/questions/programme-availability/uhd-content
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
        super().__init__(ctx)
        self.vcodec = ctx.parent.params.get("vcodec")
        self.range = ctx.parent.params.get("range_")

        self.session.headers.update({"user-agent": "BBCiPlayer/5.17.2.32046"})

        if self.range and self.range[0].name == "HLG" and not self.config.get("cert"):
            self.log.error("HLG tracks cannot be requested without an SSL certificate")
            sys.exit(1)

        elif self.range and self.range[0].name == "HLG":
            self.session.headers.update({"user-agent": self.config["user_agent"]})
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
        try:
            kind, pid = (re.match(self.TITLE_RE, self.title).group(i) for i in ("kind", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        data = self.get_data(pid, slice_id=None)
        if data is None and kind == "episode":
            return Series([self.fetch_episode(pid)])

        elif data is None:
            raise ValueError(f"Metadata was not found - if {pid} is an episode, use full URL as input")

        if not data.get("labels") or "Film" in data.get("labels", {}).get("category", ""):
            data = self.session.get(self.config["endpoints"]["episodes"].format(pid=pid)).json()
            if not data.get("episodes"):
                raise ValueError(f"Metadata was not found for {pid}")

            movie = data.get("episodes")[0]

            return Movies(
                [
                    Movie(
                        id_=movie.get("id"),
                        name=movie.get("title"),
                        year=movie.get("release_date_time", "").split("-")[0],
                        service=self.__class__,
                        language="en",
                        data=data,
                    )
                ]
            )
        else:
            seasons = [self.get_data(pid, x["id"]) for x in data["slices"] or [{"id": None}]]
            episode_ids = [
                episode["episode"].get("id")
                for season in seasons
                for episode in season["entities"]["results"]
                if not episode["episode"].get("live")
            ]
            episodes = self.get_episodes(episode_ids)
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        r = self.session.get(url=self.config["endpoints"]["playlist"].format(pid=title.id))
        r.raise_for_status()
        playlist = r.json()

        versions = playlist.get("allAvailableVersions")
        if not versions:
            # If API returns no versions, try to fetch from site source code
            r = self.session.get(self.config["base_url"].format(type="episode", pid=title.id))
            redux = re.search("window.__IPLAYER_REDUX_STATE__ = (.*?);</script>", r.text).group(1)
            data = json.loads(redux)
            versions = [{"pid": x.get("id") for x in data.get("versions", {}) if not x.get("kind") == "audio-described"}]

        if self.vcodec == "H.265":
            versions = [{"pid": playlist.get("defaultAvailableVersion", {}).get("pid")}]

        if not versions:
            self.log.error(" - No available versions for this title was found")
            sys.exit(1)

        connections = [self.check_all_versions(version) for version in (x.get("pid") for x in versions)]
        quality = [connection.get("height") for i in connections for connection in i if connection.get("height")]
        max_quality = max((h for h in quality if h < "1080"), default=None)

        media = next(
            (i for i in connections if any(connection.get("height") == max_quality for connection in i)),
            None,
        )

        if not media:
            self.log.error(" - Selection unavailable. Title doesn't exist or your IP address is blocked")
            sys.exit(1)

        connection = {}
        for video in [x for x in media if x["kind"] == "video"]:
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
            # UHD DASH manifest has no range information, so we add it manually
            if video.codec == Video.Codec.HEVC:
                video.range = Video.Range.HLG

            if any(re.search(r"-audio_\w+=\d+", x) for x in as_list(video.url)):
                # create audio stream from the video stream
                audio_url = re.sub(r"-video=\d+", "", as_list(video.url)[0])
                audio = Audio(
                    # use audio_url not video url, as to ignore video bitrate in ID
                    id_=hashlib.md5(audio_url.encode()).hexdigest()[0:7],
                    url=audio_url,
                    codec=Audio.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs),
                    language=video.data["hls"]["playlist"].media[0].language,
                    bitrate=int(self.find(r"-audio_\w+=(\d+)", as_list(video.url)[0]) or 0),
                    channels=video.data["hls"]["playlist"].media[0].channels,
                    descriptive=False,  # Not available
                    descriptor=Audio.Descriptor.HLS,
                    drm=video.drm,
                    data=video.data,
                )
                if not tracks.exists(by_id=audio.id):
                    # some video streams use the same audio, so natural dupes exist
                    tracks.add(audio)
                # remove audio from the video stream
                video.url = [re.sub(r"-audio_\w+=\d+", "", x) for x in as_list(video.url)][0]
                video.codec = Video.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs)
                video.bitrate = int(self.find(r"-video=(\d+)", as_list(video.url)[0]) or 0)

        for caption in [x for x in media if x["kind"] == "captions"]:
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

    def check_all_versions(self, vpid: str) -> list:
        media = None

        if self.vcodec == "H.265":
            if not self.config.get("cert"):
                self.log.error(" - H.265 tracks cannot be requested without an SSL certificate")
                sys.exit(1)

            session = self.session
            session.mount("https://", SSLCiphers())
            session.mount("http://", SSLCiphers())
            mediaset = "iptv-uhd"

            for mediator in ["securegate.iplayer.bbc.co.uk", "ipsecure.stage.bbc.co.uk"]:
                availability = session.get(
                    self.config["endpoints"]["secure"].format(mediator, vpid, mediaset),
                    cert=self.config["cert"],
                ).json()
                if availability.get("media"):
                    media = availability["media"]
                    break

            if availability.get("result"):
                self.log.error(f"Error: {availability['result']}")
                sys.exit(1)

        else:
            mediaset = "iptv-all"

            for mediator in ["open.live.bbc.co.uk", "open.stage.bbc.co.uk"]:
                availability = self.session.get(
                    self.config["endpoints"]["open"].format(mediator, mediaset, vpid),
                ).json()
                if availability.get("media"):
                    media = availability["media"]
                    break

            if availability.get("result"):
                self.log.error(f"Error: {availability['result']}")
                sys.exit(1)

        return media

    def fetch_episode(self, pid: str) -> Series:
        r = self.session.get(self.config["endpoints"]["episodes"].format(pid=pid))
        r.raise_for_status()

        data = json.loads(r.content)
        episode = data["episodes"][0]
        subtitle = episode.get("subtitle")
        year = episode.get("release_date_time", "").split("-")[0]
        numeric_position = episode.get("numeric_tleo_position")

        if subtitle is not None:
            series = re.finditer(r"Series (\d+):|Season (\d+):|(\d{4}/\d{2}): Episode \d+", subtitle or "")
            season_num = int(next((m.group(1) or m.group(2) or m.group(3).replace("/", "") for m in series), 0))
            if season_num == 0 and not data.get("slices"):
                season_num = 1
            number_match = re.finditer(r"(\d+)\.|Episode (\d+)", subtitle)
            number = int(next((m.group(1) or m.group(2) for m in number_match), numeric_position or 0))
            name_match = re.search(r"\d+\. (.+)", subtitle)
            name = (
                name_match.group(1)
                if name_match
                else subtitle
                if not re.search(r"Series (\d+): Episode (\d+)", subtitle)
                else ""
            )

        return Episode(
            id_=episode.get("id"),
            service=self.__class__,
            title=episode.get("title"),
            season=season_num if subtitle else 0,
            number=number if subtitle else 0,
            name=name if subtitle else "",
            language="en",
            year=year,
        )

    def get_episodes(self, episodes: list) -> list:
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = list(executor.map(self.fetch_episode, episodes))
        return [task for task in tasks if task is not None]

    def find(self, pattern, string, group=None):
        if group:
            m = re.search(pattern, string)
            if m:
                return m.group(group)
        else:
            return next(iter(re.findall(pattern, string)), None)
