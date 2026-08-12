from .base import Posting, Connector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .workday import WorkdayConnector
from .muse import MuseConnector
from .adzuna import AdzunaConnector

CONNECTORS = {
    "greenhouse": GreenhouseConnector,
    "lever": LeverConnector,
    "workday": WorkdayConnector,
    "muse": MuseConnector,
    "adzuna": AdzunaConnector,
}

__all__ = ["Posting", "Connector", "CONNECTORS"]
