from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Generator
from datetime import datetime
from typing import Any, Union
from urllib.parse import urlparse, urlunparse

import click
from click import Context
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from devine.core.manifests.dash import DASH
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series
from devine.core.tracks import Chapter, Tracks
from pywidevine.cdm import Cdm as WidevineCdm


class MY5(Service):
    """
    \b
    Service code for Channel 5's My5 streaming service (https://channel5.com).
    Credit to @Diazole(https://github.com/Diazole/my5-dl) for solving the hmac.

    \b
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input for series/films/episodes can be either complete URL or just the slug/path:
          https://www.channel5.com/the-cuckoo OR the-cuckoo OR the-cuckoo/season-1/episode-1

    \b
    Known bugs:
        - The progress bar is broken for certain DASH manifests
          See issue: https://github.com/devine-dl/devine/issues/106

    """

    ALIASES = ("channel5", "ch5", "c5")
    GEOFENCE = ("gb",)
    TITLE_RE = r"^(?:https?://(?:www\.)?channel5\.com(?:/show)?/)?(?P<id>[a-z0-9-]+)(?:/(?P<sea>[a-z0-9-]+))?(?:/(?P<ep>[a-z0-9-]+))?"

    @staticmethod
    @click.command(name="MY5", short_help="https://channel5.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> MY5:
        return MY5(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.gist = self.session.get(
            self.config["endpoints"]["gist"].format(timestamp=datetime.now().timestamp())
        ).json()

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "platform": "my5desktop",
            "friendly": "1",
            "query": self.title,
        }

        r = self.session.get(self.config["endpoints"]["search"], params=params)
        r.raise_for_status()

        results = r.json()
        for result in results["shows"]:
            yield SearchResult(
                id_=result.get("f_name"),
                title=result.get("title"),
                description=result.get("s_desc"),
                label=result.get("genre"),
                url="https://www.channel5.com/show/" + result.get("f_name"),
            )

    def get_titles(self) -> Union[Movies, Series]:
        title, season, episode = (re.match(self.TITLE_RE, self.title).group(i) for i in ("id", "sea", "ep"))
        if not title:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if season and episode:
            r = self.session.get(
                self.config["endpoints"]["single"].format(
                    show=title,
                    season=season,
                    episode=episode,
                )
            )
            r.raise_for_status()
            episode = r.json()
            return Series(
                [
                    Episode(
                        id_=episode.get("id"),
                        service=self.__class__,
                        title=episode.get("sh_title"),
                        season=int(episode.get("sea_num")) if episode.get("sea_num") else 0,
                        number=int(episode.get("ep_num")) if episode.get("ep_num") else 0,
                        name=episode.get("sh_title"),
                        language="en",
                    )
                ]
            )

        r = self.session.get(self.config["endpoints"]["episodes"].format(show=title))
        r.raise_for_status()
        data = r.json()

        if data["episodes"][0]["genre"] == "Film":
            return Movies(
                [
                    Movie(
                        id_=movie.get("id"),
                        service=self.__class__,
                        year=None,
                        name=movie.get("sh_title"),
                        language="en",  # TODO: don't assume
                    )
                    for movie in data.get("episodes")
                ]
            )
        else:
            return Series(
                [
                    Episode(
                        id_=episode.get("id"),
                        service=self.__class__,
                        title=episode.get("sh_title"),
                        season=int(episode.get("sea_num")) if episode.get("sea_num") else 0,
                        number=int(episode.get("ep_num")) if episode.get("sea_num") else 0,
                        name=episode.get("title"),
                        language="en",  # TODO: don't assume
                    )
                    for episode in data["episodes"]
                ]
            )

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        self.manifest, self.license = self.get_playlist(title.id)

        tracks = DASH.from_url(self.manifest, self.session).to_tracks(title.language)

        for track in tracks.audio:
            role = track.data["dash"]["representation"].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> list[Chapter]:
        return []

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return WidevineCdm.common_privacy_cert

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        r = self.session.post(self.license, data=challenge)
        r.raise_for_status()

        return r.content

    # Service specific functions

    def decrypt_data(self, media: str) -> dict:
        key = base64.b64decode(self.gist["key"])

        r = self.session.get(media)
        if not r.ok:
            raise ConnectionError(r.json().get("message"))

        content = r.json()

        iv = base64.urlsafe_b64decode(content["iv"])
        data = base64.urlsafe_b64decode(content["data"])

        cipher = AES.new(key=key, iv=iv, mode=AES.MODE_CBC)
        decrypted_data = unpad(cipher.decrypt(data), AES.block_size)
        return json.loads(decrypted_data)

    def get_playlist(self, asset_id: str) -> tuple:
        secret = self.gist["hmac"]

        timestamp = datetime.now().timestamp()
        vod = self.config["endpoints"]["vod"].format(id=asset_id, timestamp=f"{timestamp}")
        sig = hmac.new(base64.b64decode(secret), vod.encode(), hashlib.sha256)
        auth = base64.urlsafe_b64encode(sig.digest()).decode()
        vod += f"&auth={auth}"

        data = self.decrypt_data(vod)

        asset = [x for x in data["assets"] if x["drm"] == "widevine"][0]
        rendition = asset["renditions"][0]
        mpd_url = rendition["url"]
        lic_url = asset["keyserver"]

        parse = urlparse(mpd_url)
        path = parse.path.split("/")
        path[-1] = path[-1].split("-")[0].split("_")[0]
        manifest = urlunparse(parse._replace(path="/".join(path)))
        manifest += ".mpd" if not manifest.endswith("mpd") else ""

        return manifest, lic_url
