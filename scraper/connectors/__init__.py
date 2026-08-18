from .base import Posting, Connector
from .ashby import AshbyConnector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .workday import WorkdayConnector
from .muse import MuseConnector
from .jsonld import JsonLdConnector
from .oracle_recruiting import OracleRecruitingConnector
from .smartrecruiters import SmartRecruitersConnector

CONNECTORS = {
    "greenhouse": GreenhouseConnector,
    "ashby": AshbyConnector,
    "smartrecruiters": SmartRecruitersConnector,
    "lever": LeverConnector,
    "workday": WorkdayConnector,
    "muse": MuseConnector,
    "jsonld": JsonLdConnector,
    "oracle_recruiting": OracleRecruitingConnector,
}

__all__ = ["Posting", "Connector", "CONNECTORS"]
