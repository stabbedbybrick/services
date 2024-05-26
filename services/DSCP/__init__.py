from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union

import click
from click import Context
from devine.core.credential import Credential
from devine.core.manifests.dash import DASH
from devine.core.search_result import SearchResult
from devine.core.service import Service
from devine.core.titles import Episode, Movie, Movies, Series
from devine.core.tracks import Chapter, Tracks


class DSCP(Service):
    """
    \b
    Service code for Discovery Plus (https://discoveryplus.com).

    \b
    Author: stabbedbybrick
    Authorization: Cookies
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input can be either complete title URL or just the path: '/show/richard-hammonds-workshop'
        - Use the --lang LANG_RANGE option to request non-english tracks
        - Single video URLs are currently not supported

    """

    ALIASES = ("dplus", "discoveryplus", "discovery+")
    TITLE_RE = r"^(?:https?://(?:www\.)?discoveryplus\.com(?:/[a-z]{2})?)?/(?P<type>show|video)/(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="DSCP", short_help="https://discoveryplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSCP:
        return DSCP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str):
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
        self.configure()

    def search(self) -> Generator[SearchResult, None, None]:
        r = self.session.get(self.config["endpoints"]["search"].format(region=self.region, query=self.title))
        r.raise_for_status()
        data = r.json()

        results = [x.get("attributes") for x in data["included"] if x.get("type") == "show"]

        for result in results:
            yield SearchResult(
                id_=f"/show/{result.get('alternateId')}",
                title=result.get("name"),
                description=result.get("description"),
                label="show",
                url=f"/show/{result.get('alternateId')}",
            )

    def get_titles(self) -> Union[Movies, Series]:
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if kind == "video":
            self.log.error("Single videos are not supported by this service.")
            sys.exit(1)

        if kind == "show":
            r = self.session.get(self.config["endpoints"]["show"].format(region=self.region, title_id=content_id))
            r.raise_for_status()
            data = r.json()

            content = next(x for x in data["included"] if x["attributes"].get("alias") == "generic-show-episodes")
            content_id = content["id"]
            show_id = content["attributes"]["component"]["mandatoryParams"]
            season_params = [x.get("parameter") for x in content["attributes"]["component"]["filters"][0]["options"]]
            page = next(x for x in data["included"] if x.get("type", "") == "page")

            seasons = [
                self.session.get(
                    self.config["endpoints"]["seasons"].format(
                        region=self.region, content_id=content_id, season=season, show_id=show_id
                    )
                ).json()
                for season in season_params
            ]

            videos = [[x for x in season["included"] if x["type"] == "video"] for season in seasons]

            return Series(
                [
                    Episode(
                        id_=ep["id"],
                        service=self.__class__,
                        title=page["attributes"]["title"],
                        year=ep["attributes"]["airDate"][:4],
                        season=ep["attributes"].get("seasonNumber"),
                        number=ep["attributes"].get("episodeNumber"),
                        name=ep["attributes"]["name"],
                        language=ep["attributes"]["audioTracks"][0]
                        if ep["attributes"].get("audioTracks")
                        else self.user_language,
                        data=ep,
                    )
                    for episodes in videos
                    for ep in episodes
                    if ep["attributes"]["videoType"] == "EPISODE"
                ]
            )

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        res = self.session.post(
            self.config["endpoints"]["playback"].format(region=self.region),
            json={
                "videoId": title.id,
                "wisteriaProperties": {
                    "advertiser": {
                        "adId": "|84958235701907329361495486486652228049||17163182474853637414c74993b0cb4f9a42062d41449",
                        "firstPlay": 0,
                        "fwDid": "",
                        "fwIsLat": 0,
                        "interactiveCapabilities": [
                            "brightline",
                        ],
                    },
                    "appBundle": "undefined",
                    "device": {
                        "browser": {
                            "name": "chrome",
                            "version": "125.0.0.0",
                        },
                        "id": "",
                        "language": "en",
                        "make": "",
                        "model": "",
                        "name": "chrome",
                        "os": "Windows",
                        "osVersion": "NT 10.0",
                        "player": {
                            "name": "Discovery Player Web",
                            "version": "",
                        },
                        "type": "desktop",
                    },
                    "gdpr": 0,
                    "platform": "desktop",
                    "playbackId": str(uuid.uuid4()),
                    "product": "dplus_se" if self.site_id != "dplus_se" else "dplus_us",
                    "sessionId": str(uuid.uuid4()),
                    "siteId": "dplus_se" if self.site_id != "dplus_se" else "dplus_us",
                    "streamProvider": {
                        "hlsVersion": 6,
                        "pingConfig": 0,
                        "suspendBeaconing": 0,
                        "version": "1.0.0",
                    },
                },
                "deviceCapabilities": {
                    "manifests": {
                        "formats": {
                            "dash": {},
                        },
                    },
                    "segments": {
                        "formats": {
                            "fmp4": {},
                        },
                    },
                    "codecs": {
                        "audio": {
                            "decoders": [
                                {
                                    "codec": "aac",
                                    "profiles": [
                                        "lc",
                                        "hev",
                                        "hev2",
                                    ],
                                },
                            ],
                        },
                        "video": {
                            "decoders": [
                                {
                                    "codec": "h264",
                                    "profiles": [
                                        "high",
                                        "main",
                                        "baseline",
                                    ],
                                    "maxLevel": "5.2",
                                },
                                {
                                    "codec": "h265",
                                    "profiles": [
                                        "main10",
                                        "main",
                                    ],
                                    "maxLevel": "5.2",
                                },
                            ],
                            "hdrFormats": [],
                        },
                    },
                    "contentProtection": {
                        "contentDecryptionModules": [
                            {
                                "drmKeySystem": "clearkey",
                            },
                            {
                                "drmKeySystem": "widevine",
                                "maxSecurityLevel": "l3",
                            },
                        ],
                    },
                },
                "deviceInfo": {
                    "adBlocker": False,
                    "deviceId": "",
                    "drmTypes": {
                        "widevine": True,
                        "playready": False,
                        "fairplay": False,
                        "clearkey": True,
                    },
                    "drmSupported": True,
                    "hdrCapabilities": [
                        "SDR",
                    ],
                    "hwDecodingCapabilities": [
                        "H264",
                        "H265",
                    ],
                    "soundCapabilities": [
                        "STEREO",
                    ],
                },
            },
        ).json()

        if "errors" in res:
            if "missingpackage" in res["errors"][0]["code"]:
                self.log.error("- Access Denied. Please check your subscription.")
                sys.exit(1)

            if "invalid.token" in res["errors"][0]["code"]:
                self.log.error("- Invalid Token. Cookies are invalid or may have expired.")
                sys.exit(1)

            raise ConnectionError(res["errors"])

        streaming = res["data"]["attributes"]["streaming"][0]

        manifest = streaming["url"]
        if streaming["protection"]["drmEnabled"]:
            self.token = streaming["protection"]["drmToken"]
            self.license = streaming["protection"]["schemes"]["widevine"]["licenseUrl"]

        tracks = DASH.from_url(url=manifest, session=self.session).to_tracks(language=title.language)

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> list[Chapter]:
        return []

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, challenge: bytes, **_: Any) -> str:
        if not self.license:
            return None

        r = self.session.post(self.license, headers={"Preauthorization": self.token}, data=challenge)
        if not r.ok:
            raise ConnectionError(r.text)

        return r.content

    # Service specific functions

    def configure(self):
        self.session.headers.update(
            {
                "origin": "https://www.discoveryplus.com",
                "referer": "https://www.discoveryplus.com/",
                "x-disco-client": "WEB:UNKNOWN:dplus_us:2.44.4",
                "x-disco-params": "realm=go,siteLookupKey=dplus_us,bid=dplus,hn=www.discoveryplus.com,hth=,uat=false",
            }
        )

        info = self.session.get(self.config["endpoints"]["info"]).json()
        self.region = info["data"]["attributes"]["baseApiUrl"].split("-")[0].split("//")[1]

        user = self.session.get(self.config["endpoints"]["user"].format(region=self.region)).json()
        if "errors" in user:
            raise ConnectionError(user["errors"])

        self.territory = user["data"]["attributes"]["currentLocationTerritory"]
        self.user_language = user["data"]["attributes"]["clientTranslationLanguageTags"][0]
        self.site_id = user["meta"]["site"]["id"]
