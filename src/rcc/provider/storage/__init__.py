from .storage_provider import StorageProvider
from .s3 import S3


def from_config(cfg):
    name = cfg.provider["storage"].lower()
    if name == "s3":
        return S3(cfg)
    else:
        raise ValueError("Unknown provider '{}'".format(cfg.provider["storage"]))
