from .base import Posting, Connector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .workday import WorkdayConnector
from .muse import MuseConnector
from .adzuna import AdzunaConnector
from .jsonld import JsonLdConnector

CONNECTORS = {
    "greenhouse": GreenhouseConnector,
    "lever": LeverConnector,
    "workday": WorkdayConnector,
    "muse": MuseConnector,
    "adzuna": AdzunaConnector,
    "jsonld": JsonLdConnector,
}

__all__ = ["Posting", "Connector", "CONNECTORS"]
