from __future__ import annotations

import json
import re
import sys
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
from urllib.parse import urljoin

import click
from click import Context
from devine.core.constants import AnyTrack
from devine.core.credential import Credential
from devine.core.manifests import DASH, HLS
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series
from devine.core.tracks import Chapter, Chapters, Tracks
from requests import Request


class CBC(Service):
    """
    \b
    Service code for CBC Gem streaming service (https://gem.cbc.ca/).

    \b
    Author: stabbedbybrick
    Authorization: Credentials
    Robustness:
      AES-128: 1080p, DDP5.1
      Widevine: 720p, DDP5.1

    \b
    Tips:
        - Input can be complete title URL or just the slug:
          SHOW: https://gem.cbc.ca/murdoch-mysteries OR murdoch-mysteries
          MOVIE: https://gem.cbc.ca/the-babadook OR the-babadook

    \b
    Notes:
        - CCExtrator v0.94 will likely fail to extract subtitles. It's recommended to downgrade to v0.93.
        - Some audio tracks contain invalid data, causing warning messages from mkvmerge during muxing
          These can be ignored.

    """

    GEOFENCE = ("ca",)
    ALIASES = ("gem", "cbcgem",)

    @staticmethod
    @click.command(name="CBC", short_help="https://gem.cbc.ca/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> CBC:
        return CBC(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
        self.title = title
        super().__init__(ctx)

        self.base_url = self.config["endpoints"]["base_url"]

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "device": "web",
            "pageNumber": "1",
            "pageSize": "20",
            "term": self.title,
        }
        response = self._request("GET", "/ott/catalog/v1/gem/search", params=params)

        for result in response.get("result", []):
            yield SearchResult(
                id_="https://gem.cbc.ca/{}".format(result.get("url")),
                title=result.get("title"),
                description=result.get("synopsis"),
                label=result.get("type"),
                url="https://gem.cbc.ca/{}".format(result.get("url")),
            )

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        login = self.cache.get(f"login_{credential.sha1}")
        tokens = self.cache.get(f"tokens_{credential.sha1}")

        if login and not login.expired:
            # cached
            self.log.info(" + Using cached login tokens")
            auth_token = login.data["access_token"]

        elif login and login.expired:
            payload = {
                "email": credential.username,
                "password": credential.password,
                "refresh_token": login.data["refresh_token"],
            }

            params = {"apikey": self.config["endpoints"]["api_key"]}
            auth = self._request(
                "POST", "https://api.loginradius.com/identity/v2/auth/login",
                payload=payload,
                params=params,
            )

            login.set(auth, expiration=auth["expires_in"])
            auth_token = login.data["access_token"]

            self.log.info(" + Refreshed login tokens")

        else:
            payload = {
                "email": credential.username,
                "password": credential.password,
            }
            params = {"apikey": self.config["endpoints"]["api_key"]}
            auth = self._request(
                "POST", "https://api.loginradius.com/identity/v2/auth/login",
                payload=payload,
                params=params,
            )

            login.set(auth, expiration=auth["expires_in"])
            auth_token = login.data["access_token"]

            self.log.info(" + Acquired fresh login tokens")

        if tokens and not tokens.expired:
            # cached
            self.log.info(" + Using cached access tokens")
            access_token = tokens.data["accessToken"]

        else:
            access = self.access_token(auth_token)
            tokens.set(access, expiration=access["accessTokenExpiresIn"])
            access_token = access["accessToken"]
            self.log.info(" + Acquired fresh access tokens")

        claims_token = self.claims_token(access_token)
        self.session.headers.update({"x-claims-token": claims_token})

    def get_titles(self) -> Union[Movies, Series]:
        title_re = r"^(?:https?://(?:www.)?gem.cbc.ca/)?(?P<id>[a-zA-Z0-9_-]+)"
        try:
            title_id = re.match(title_re, self.title).group("id")
        except Exception:
            raise ValueError("- Could not parse ID from title")

        data = self._request("GET", "/ott/cbc-api/v2/shows/{}".format(title_id))
        label = data.get("seasons", [])[0].get("title")

        if label.lower() in ("film", "movie"):
            movie = self._movie(data)
            return Movies(movie)

        else:
            episodes = self._show(data)
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        media_id = title.data["playSession"].get("mediaId")
        index = self._request(
            "GET", "/media/meta/v1/index.ashx",
            params={"appCode": "gem", "idMedia": media_id, "output": "jsonObject"}
        )

        title.data["extra"] = {
            "chapters": index["Metas"].get("Chapitres"),
            "credits": index["Metas"].get("CreditStartTime"),
        }

        self.drm = index["Metas"].get("isDrmActive") == "true"
        if self.drm:
            tech = next(tech["name"] for tech in index["availableTechs"] if "widevine" in tech["drm"])
        else:
            tech = next(tech["name"] for tech in index["availableTechs"] if not tech["drm"])

        response = self._request(
            "GET", self.config["endpoints"]["validation"].format("android", media_id, "smart-tv", tech)
        )

        manifest = response.get("url")
        self.license = next((x["value"] for x in response["params"] if "widevineLicenseUrl" in x["name"]), None)
        self.token = next((x["value"] for x in response["params"] if "widevineAuthToken" in x["name"]), None)

        stream_type = HLS if tech == "hls" else DASH
        tracks = stream_type.from_url(manifest, self.session).to_tracks(language=index.get("Language", "en"))

        if stream_type == DASH:
            for track in tracks.audio:
                label = track.data["dash"]["adaptation_set"].find("Label")
                if label is not None and "descriptive" in label.text.lower():
                    track.descriptive = True

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        extra = title.data["extra"]

        chapters = []
        if extra.get("chapters"):
            chapters = [Chapter(timestamp=x) for x in set(extra["chapters"].split(","))]

        if extra.get("credits"):
            chapters.append(Chapter(name="Credits", timestamp=float(extra["credits"])))

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(
        self, *, challenge: bytes, title: Union[Movies, Series], track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        if not self.license or not self.token:
            return None

        headers = {"x-dt-auth-token": self.token}
        r = self.session.post(self.license, headers=headers, data=challenge)
        r.raise_for_status()
        return r.content

    # Service specific

    def _show(self, data: dict) -> Episode:
        episodes = [episode for season in data["seasons"] for episode in season["assets"] if not episode["isTrailer"]]

        return Series(
            [
                Episode(
                    id_=episode["id"],
                    service=self.__class__,
                    title=data.get("title"),
                    season=int(episode.get("season", 0)),
                    number=int(episode.get("episode", 0)),
                    name=episode.get("title"),
                    data=episode,
                )
                for episode in episodes
            ]
        )

    def _movie(self, data: dict) -> Movie:
        movies = [movie for season in data["seasons"] for movie in season["assets"] if not movie["isTrailer"]]

        return [
            Movie(
                id_=movie.get("id"),
                service=self.__class__,
                name=data.get("title"),
                data=movie,
            )
            for movie in movies
        ]

    def access_token(self, token: str) -> str:
        params = {
            "access_token": token,
            "apikey": self.config["endpoints"]["api_key"],
            "jwtapp": "jwt",
        }

        headers = {"content-type": "application/json"}
        resp = self._request(
            "GET", "https://cloud-api.loginradius.com/sso/jwt/api/token",
            headers=headers,
            params=params
        )

        payload = {"jwt": resp.get("signature")}
        headers = {"content-type": "application/json", "ott-device-type": "web"}
        auth = self._request("POST", "/ott/cbc-api/v2/token", headers=headers, payload=payload)

        return auth

    def claims_token(self, token: str) -> str:
        headers = {
            "content-type": "application/json",
            "ott-device-type": "web",
            "ott-access-token": token,
        }
        response = self._request("GET", "/ott/cbc-api/v2/profile", headers=headers)
        return response["claimsToken"]

    def _request(
        self, method: str, api: str, params: dict = None, headers: dict = None, payload: dict = None
    ) -> Any[dict | str]:
        url = urljoin(self.base_url, api)
        headers = headers or self.session.headers

        prep = self.session.prepare_request(Request(method, url, params=params, headers=headers, json=payload))
        response = self.session.send(prep)
        if response.status_code not in (200, 426):
            raise ConnectionError(f"{response.status_code} - {response.text}")

        try:
            data = json.loads(response.content)
            error_keys = ["errorMessage", "ErrorMessage", "ErrorCode", "errorCode", "error"]
            error_message = next((data.get(key) for key in error_keys if key in data), None)
            if error_message:
                self.log.error(f"\n - Error: {error_message}\n")
                sys.exit(1)

            return data

        except json.JSONDecodeError:
            raise ConnectionError("Request for {} failed: {}".format(response.url, response.text))
