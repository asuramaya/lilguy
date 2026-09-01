from .base import Posting, Connector
from .ashby import AshbyConnector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .workday import WorkdayConnector
from .muse import MuseConnector
from .jsonld import JsonLdConnector
from .oracle_recruiting import OracleRecruitingConnector
from .smartrecruiters import SmartRecruitersConnector
from .rippling import RipplingConnector
from .workable import WorkableConnector
from .bamboohr import BambooHRConnector
from .taleo import TaleoConnector
from .icims import IcimsConnector
from .ukg import UkgConnector

CONNECTORS = {
    "greenhouse": GreenhouseConnector,
    "ashby": AshbyConnector,
    "smartrecruiters": SmartRecruitersConnector,
    "lever": LeverConnector,
    "workday": WorkdayConnector,
    "muse": MuseConnector,
    "jsonld": JsonLdConnector,
    "oracle_recruiting": OracleRecruitingConnector,
    "rippling": RipplingConnector,
    "workable": WorkableConnector,
    "bamboohr": BambooHRConnector,
    "taleo": TaleoConnector,
    "icims": IcimsConnector,
    "ukg": UkgConnector,
}

__all__ = ["Posting", "Connector", "CONNECTORS"]
