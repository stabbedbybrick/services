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
from requests import Request
from unshackle.core.cdm.detect import is_playready_cdm
from unshackle.core.constants import AnyTrack
from unshackle.core.credential import Credential
from unshackle.core.manifests import DASH, HLS
from unshackle.core.search_result import SearchResult
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series
from unshackle.core.tracks import Chapter, Chapters, Tracks


class CBC(Service):
    """
    \b
    Service code for CBC Gem streaming service (https://gem.cbc.ca/).

    \b
    Version: 1.0.2
    Author: stabbedbybrick
    Authorization: Credentials
    Robustness:
        AES-128: 1080p
        Widevine:
            L3: 1080p
        PlayReady:
            SL2000: 1080p

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

        self.drm_system = "playready" if is_playready_cdm(ctx.obj.cdm) else "widevine"

        self.base_url = self.config["endpoints"]["base_url"]

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "device": "web",
            "pageNumber": "1",
            "pageSize": "20",
            "term": self.title,
        }
        response = self._request("GET", "/ott/catalog/v2/gem/search", params=params)

        for result in response.get("results", []):
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

        tokens = self.cache.get(f"tokens_{credential.sha1}")

        """
        All grant types for future reference:
            PASSWORD("password"),
            ACCESS_TOKEN("access_token"),
            REFRESH_TOKEN("refresh_token"),
            CLIENT_CREDENTIALS("client_credentials"),
            AUTHORIZATION_CODE("authorization_code"),
            CODE("code");
        """

        if tokens and not tokens.expired:
            # cached
            self.log.info(" + Using cached tokens")
            auth_token = tokens.data["access_token"]

        elif tokens and tokens.expired:
            # expired, refresh
            self.log.info("Refreshing cached tokens...")
            auth_url, scopes = self.settings()
            params = {
                "client_id": self.config["client"]["id"],
                "grant_type": "refresh_token",
                "refresh_token": tokens.data["refresh_token"],
                "scope": scopes,
            }

            access = self._request("POST", auth_url, params=params)

            # Shorten expiration by one hour to account for clock skew
            tokens.set(access, expiration=int(access["expires_in"]) - 3600)
            auth_token = access["access_token"]

        else:
            # new
            self.log.info("Requesting new tokens...")
            auth_url, scopes = self.settings()
            params = {
                "client_id": self.config["client"]["id"],
                "grant_type": "password",
                "username": credential.username,
                "password": credential.password,
                "scope": scopes,
            }

            access = self._request("POST", auth_url, params=params)

            # Shorten expiration by one hour to account for clock skew
            tokens.set(access, expiration=int(access["expires_in"]) - 3600)
            auth_token = access["access_token"]

        claims_token = self.claims_token(auth_token)
        self.session.headers.update({"x-claims-token": claims_token})

    def get_titles(self) -> Union[Movies, Series]:
        title_re= r"^(?:https?://(?:www.)?gem.cbc.ca/)?(?P<id>[a-zA-Z0-9_-]+)"
        try:
            title_id = re.match(title_re, self.title).group("id")
        except Exception:
            raise ValueError("- Could not parse ID from title")

        params = {"device": "web"}
        data = self._request("GET", "/ott/catalog/v2/gem/show/{}".format(title_id), params=params)
        label = data.get("contentType", "").lower()

        if label in ("film", "movie", "standalone"):
            movies = self._movie(data)
            return Movies(movies)

        else:
            episodes = self._show(data)
            return Series(episodes)

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        index = self._request(
            "GET", "/media/meta/v1/index.ashx",
            params={"appCode": "gem", "idMedia": title.id, "output": "jsonObject"}
        )

        title.data["extras"] = {
            "chapters": index["Metas"].get("Chapitres"),
            "credits": index["Metas"].get("CreditStartTime"),
        }

        is_drm = index["Metas"].get("isDrmActive") == "true"
        if is_drm:
            tech = next((t["name"] for t in index["availableTechs"] if self.drm_system in t.get("drm", [])), None)
        else:
            tech = next((t["name"] for t in index["availableTechs"] if not t.get("drm")), None)

        if not tech:
            raise ValueError(f"Could not find a compatible tech for {title}")

        params = {
            "appCode": "gem",
            "connectionType": "hd",
            "deviceType": "android",
            "idMedia": title.id,
            "multibitrate": "true",
            "output": "json",
            "tech": tech,
            "manifestVersion": "2",
            "manifestType": "smart-tv",
        }

        response = self._request("GET", self.config["endpoints"]["validation"], params=params)
        
        manifest = response.get("url")

        title.data["is_drm"] = is_drm
        title.data["license_params"] = response.get("params")

        stream_type = HLS if tech == "hls" else DASH
        tracks = stream_type.from_url(manifest, self.session).to_tracks(language=title.language)

        if stream_type == DASH:
            for track in tracks.audio:
                label = track.data["dash"]["adaptation_set"].find("Label")
                if label is not None and "descriptive" in label.text.lower():
                    track.descriptive = True

        for track in tracks:
            track.language = title.language

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        if not (extras := title.data.get("extras")):
            return Chapters()

        chapters = []
        if extras.get("chapters"):
            chapters = [Chapter(timestamp=x) for x in set(extras["chapters"].split(","))]

        if extras.get("credits"):
            chapters.append(Chapter(name="Credits", timestamp=float(extras["credits"])))

        return Chapters(chapters)

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(
        self, *, challenge: bytes, title: Union[Movies, Series], track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        if not title.data.get("is_drm"):
            return None
        
        if not (params := title.data.get("license_params")):
            raise ValueError("Title is DRM protected but no license params found")
        
        license_url = next((x["value"] for x in params if "widevineLicenseUrl" in x["name"]), None)
        token = next((x["value"] for x in params if "widevineAuthToken" in x["name"]), None)

        if not license_url or not token:
            raise ValueError("Title is DRM protected but no license url or token found")

        headers = {"x-dt-auth-token": token}
        r = self.session.post(license_url, headers=headers, data=challenge)
        r.raise_for_status()
        return r.content
    
    def get_playready_license(
        self, *, challenge: bytes, title: Union[Movies, Series], track: AnyTrack
    ) -> Optional[Union[bytes, str]]:
        if not title.data.get("is_drm"):
            return None
        
        if not (params := title.data.get("license_params")):
            raise ValueError("Title is DRM protected but no license params found")
        
        license_url = next((x["value"] for x in params if "playreadyLicenseUrl" in x["name"]), None)
        token = next((x["value"] for x in params if "playreadyAuthToken" in x["name"]), None)

        if not license_url or not token:
            raise ValueError("Title is DRM protected but no license url or token found")

        headers = {"x-dt-auth-token": token}
        r = self.session.post(license_url, headers=headers, data=challenge)
        r.raise_for_status()
        return r.content

    # Service-specific methods

    def _show(self, data: dict) -> list[Episode]:
        lineups = next((x["lineups"] for x in data["content"] if x.get("title", "").lower() in ("episodes", "parts")), None)
        if not lineups:
            self.log.warning("No episodes found for: {}".format(data.get("title")))
            return

        titles = []
        for season in lineups:
            for episode in season["items"]:
                if episode.get("mediaType", "").lower() == "episode":
                    parts = episode.get("title", "").split(".", 1)
                    episode_name = parts[1].strip() if len(parts) > 1 else parts[0].strip()
                    titles.append(
                        Episode(
                            id_=episode["idMedia"],
                            service=self.__class__,
                            title=data.get("title"),
                            season=int(season.get("seasonNumber", 0)),
                            number=int(episode.get("episodeNumber", 0)),
                            name=episode_name,
                            year=episode.get("metadata", {}).get("productionYear"),
                            language=data["structuredMetadata"].get("inLanguage", "en-CA"),
                            data=episode,
                        )
                    )

        return titles

    def _movie(self, data: dict) -> list[Movie]:
        unwanted = ("episodes", "trailers", "extras")
        lineups = next((x["lineups"] for x in data["content"] if x.get("title", "").lower() not in unwanted), None)
        if not lineups:
            self.log.warning("No movies found for: {}".format(data.get("title")))
            return

        titles = []
        for season in lineups:
            for movie in season["items"]:
                if movie.get("mediaType", "").lower() == "episode":
                    parts = movie.get("title", "").split(".", 1)
                    movie_name = parts[1].strip() if len(parts) > 1 else parts[0].strip()
                    titles.append(
                        Movie(
                            id_=movie.get("idMedia"),
                            service=self.__class__,
                            name=movie_name,
                            year=movie.get("metadata", {}).get("productionYear"),
                            language=data["structuredMetadata"].get("inLanguage", "en-CA"),
                            data=movie,
                        )
                    )

        return titles
    
    def settings(self) -> tuple:
        settings = self._request("GET", "/ott/catalog/v1/gem/settings", params={"device": "web"})
        auth_url = settings["identityManagement"]["ropc"]["url"]
        scopes = settings["identityManagement"]["ropc"]["scopes"]
        return auth_url, scopes

    def claims_token(self, token: str) -> str:
        headers = {
            "Authorization": "Bearer " + token,
        }
        params = {"device": "web"}
        response = self._request(
            "GET", "/ott/subscription/v2/gem/Subscriber/profile", headers=headers, params=params
        )
        return response["claimsToken"]

    def _request(self, method: str, api: str, **kwargs: Any) -> Any[dict | str]:
        url = urljoin(self.base_url, api)

        prep: Request = self.session.prepare_request(Request(method, url, **kwargs))
        response = self.session.send(prep)
        if response.status_code not in (200, 426):
            raise ConnectionError(f"{response.status_code} - {response.text}")

        try:
            data = json.loads(response.content)
            error_keys = ["errorMessage", "ErrorMessage", "ErrorCode", "errorCode", "error"]
            error_code = next((data.get(key) for key in error_keys if key in data), None)
            if error_code:
                self.log.error(f"\n ErrorCode: {error_code} - {data.get('message')} \n")
                sys.exit(1)

            return data

        except json.JSONDecodeError:
            raise ConnectionError("Request for {} failed: {}".format(response.url, response.text))
