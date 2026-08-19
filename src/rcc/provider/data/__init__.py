from typing import cast

from ...config import Config
from .data_provider import DataProvider as DataProvider
from .postgres import Postgres as Postgres


def from_config(cfg: Config) -> DataProvider:
    provider_cfg = cast(dict[str, object], cfg.provider)
    name = str(provider_cfg["data"]).lower()
    if name == "postgres":
        return Postgres(cfg)
    else:
        raise ValueError("Unknown provider '{}'".format(provider_cfg["data"]))
