from .data_provider import DataProvider
from .postgres import Postgres


def from_config(cfg):
    name = cfg.provider["data"].lower()
    if name == "postgres":
        return Postgres(cfg)
    else:
        raise ValueError("Unknown provider '{}'".format(cfg.provider["data"]))
