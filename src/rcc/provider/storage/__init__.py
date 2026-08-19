from typing import cast

from ...config import Config
from .s3 import S3 as S3
from .storage_provider import StorageProvider as StorageProvider


def from_config(cfg: Config) -> StorageProvider:
    provider_cfg = cast(dict[str, object], cfg.provider)
    name = str(provider_cfg["storage"]).lower()
    if name == "s3":
        return S3(cfg)
    else:
        raise ValueError("Unknown provider '{}'".format(provider_cfg["storage"]))
