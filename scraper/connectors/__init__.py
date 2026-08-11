from .base import Posting, Connector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .workday import WorkdayConnector

CONNECTORS = {
    "greenhouse": GreenhouseConnector,
    "lever": LeverConnector,
    "workday": WorkdayConnector,
}

__all__ = ["Posting", "Connector", "CONNECTORS"]
