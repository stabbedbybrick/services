from __future__ import annotations

import hashlib
import re
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import urljoin

import click
import m3u8
from devine.core.credential import Credential
from devine.core.downloaders import requests
from devine.core.manifests import HLS
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from devine.core.tracks import Chapter, Subtitle, Track, Tracks
from langcodes import Language


class TUBI(Service):
    """
    Service code for TubiTV streaming service (https://tubitv.com/)

    \b
    Author: stabbedbybrick
    Authorization: Cookies
    Robustness:
      Widevine:
        L3: 720p, AAC2.0

    \b
    Tips:
        - Input can be complete title URL or just the path:
            /series/300001423/gotham
            /tv-shows/200024793/s01-e01-pilot
            /movies/589279/the-outsiders
    """

    TITLE_RE = r"^(?:https?://(?:www\.)?tubitv\.com?)?/(?P<type>movies|series|tv-shows)/(?P<id>[a-z0-9-]+)"
    GEOFENCE = ("us", "ca",)

    @staticmethod
    @click.command(name="TUBI", short_help="https://tubitv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TUBI(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.license = None

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if not cookies:
            raise EnvironmentError("Service requires Cookies for Authentication.")
        
        self.session.cookies.update(cookies)

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "isKidsMode": "false",
            "useLinearHeader": "true",
            "isMobile": "false",
        }

        r = self.session.get(self.config["endpoints"]["search"].format(query=self.title), params=params)
        r.raise_for_status()
        results = r.json()

        for result in results:
            label = "series" if result["type"] == "s" else "movies" if result["type"] == "v" else result["type"]
            title = (
                result.get("title", "")
                .lower()
                .replace(" ", "-")
                .replace(":", "")
                .replace("(", "")
                .replace(")", "")
                .replace(".", "")
            )
            yield SearchResult(
                id_=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
                title=result.get("title"),
                description=result.get("description"),
                label=label,
                url=f"https://tubitv.com/{label}/{result.get('id')}/{title}",
            )

    def get_titles(self) -> Titles_T:
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if kind == "tv-shows":
            content = self.session.get(self.config["endpoints"]["content"].format(content_id=content_id))
            content.raise_for_status()
            series_id = "0" + content.json().get("series_id")
            data = self.session.get(self.config["endpoints"]["content"].format(content_id=series_id)).json()

            return Series(
                [
                    Episode(
                        id_=episode["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(season["id"]),
                        number=int(episode["episode_number"]),
                        name=episode["title"].split("-")[1],
                        year=data["year"],
                        language=Language.find(episode.get("lang", "en")).to_alpha3(),
                        data=episode,
                    )
                    for season in data["children"]
                    for episode in season["children"]
                    if episode["id"] == content_id
                ]
            )

        if kind == "series":
            r = self.session.get(self.config["endpoints"]["content"].format(content_id=content_id))
            r.raise_for_status()
            data = r.json()

            return Series(
                [
                    Episode(
                        id_=episode["id"],
                        service=self.__class__,
                        title=data["title"],
                        season=int(season["id"]),
                        number=int(episode["episode_number"]),
                        name=episode["title"].split("-")[1],
                        year=data["year"],
                        language=Language.find(episode.get("lang", "en")).to_alpha3(),
                        data=episode,
                    )
                    for season in data["children"]
                    for episode in season["children"]
                ]
            )

        if kind == "movies":
            r = self.session.get(self.config["endpoints"]["content"].format(content_id=content_id))
            r.raise_for_status()
            data = r.json()
            return Movies(
                [
                    Movie(
                        id_=data["id"],
                        service=self.__class__,
                        year=data["year"],
                        name=data["title"],
                        language=Language.find(data.get("lang", "en")).to_alpha3(),
                        data=data,
                    )
                ]
            )

    def get_tracks(self, title: Title_T) -> Tracks:
        if not title.data.get("video_resources"):
            raise ValueError("No video resources found. Title is either missing or geolocation is incorrect.")

        self.manifest = title.data["video_resources"][0]["manifest"]["url"]
        self.license = title.data["video_resources"][0].get("license_server", {}).get("url")

        tracks = HLS.from_url(url=self.manifest, session=self.session).to_tracks(language=title.language)
        for track in tracks:
            master = m3u8.loads(self.session.get(track.url).text, uri=track.url)
            track.url = urljoin(master.base_uri, master.segments[0].uri)
            track.descriptor = Track.Descriptor.URL

        if title.data.get("subtitles"):
            tracks.add(
                Subtitle(
                    id_=hashlib.md5(title.data["subtitles"][0]["url"].encode()).hexdigest()[0:6],
                    url=title.data["subtitles"][0]["url"],
                    codec=Subtitle.Codec.from_mime(title.data["subtitles"][0]["url"][-3:]),
                    language=title.data["subtitles"][0].get("lang_alpha3", title.language),
                    downloader=requests,
                    is_original_lang=True,
                    forced=False,
                    sdh=False,
                )
            )
        return tracks

    def get_chapters(self, title: Title_T) -> list[Chapter]:
        return []

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> bytes:
        if not self.license:
            return None

        r = self.session.post(url=self.license, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        return r.content
