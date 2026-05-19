from typing import Literal

from sqlmodel import Session

from app.util.cache import StringConfigCache

GoodreadsConfigKey = Literal[
    "goodreads_rss_url",
    "goodreads_last_polled",
    "goodreads_auto_download",
]


class GoodreadsConfig(StringConfigCache[GoodreadsConfigKey]):
    """Configuration for Goodreads shelf RSS polling."""

    def get_rss_url(self, session: Session) -> str | None:
        return self.get(session, "goodreads_rss_url")

    def set_rss_url(self, session: Session, url: str):
        self.set(session, "goodreads_rss_url", url.strip())

    def get_last_polled(self, session: Session) -> str | None:
        return self.get(session, "goodreads_last_polled")

    def set_last_polled(self, session: Session, value: str):
        self.set(session, "goodreads_last_polled", value)

    def get_auto_download(self, session: Session) -> bool:
        val = self.get_bool(session, "goodreads_auto_download")
        return val if val is not None else True

    def set_auto_download(self, session: Session, value: bool):
        self.set_bool(session, "goodreads_auto_download", value)


goodreads_config = GoodreadsConfig()
