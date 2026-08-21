"""Standardizes postings across multiple dimensions:
  1. Job Function classification (Software, Data/AI, Hardware, Mechanical, Product, Finance, Marketing, Sales, Supply Chain, Healthcare, HR, Legal, Civil, Consulting)
  2. Work Arrangement normalization (remote, hybrid, onsite)
  3. Location normalization (City, ST / City, Country / Multiple Locations / Remote)
  4. Cycle season and year extraction (Summer, Fall, Winter, Spring + 2024..2030)
  5. Title formatting cleanup (removes requisition IDs, French/English splits, all-caps)
  6. Company display name resolution (strips ATS tenant numbers, legal suffixes, maps slugs)
"""

import re
from typing import Optional

# =====================================================================
# 1. JOB FUNCTION TAXONOMY
# =====================================================================

JOB_FUNCTIONS = (
    "Software Engineering",
    "Data, AI & Machine Learning",
    "Hardware & Electrical Engineering",
    "Mechanical & Aerospace Engineering",
    "Product Management & Design",
    "Finance, Accounting & Trading",
    "Marketing & Communications",
    "Sales & Business Development",
    "Supply Chain, Logistics & Operations",
    "Healthcare, Biotech & Life Sciences",
    "Human Resources & Recruiting",
    "Legal, Policy & Compliance",
    "Civil & Environmental Engineering",
    "General Business & Consulting",
    "Other"
)

JOB_FUNCTION_RULES = [
    ("Software Engineering", [
        r"\b(?:software|swe|frontend|front-end|backend|back-end|fullstack|full-stack|web dev|mobile dev|ios|android|firmware|embedded software|cloud engineer|devops|sre|site reliability|systems engineer|platform engineer|infrastructure engineer|qa engineer|test engineer|automation engineer|security engineer|cybersecurity|application engineer|developer|it intern|business it)\b"
    ]),
    ("Data, AI & Machine Learning", [
        r"\b(?:data engineer|data science|data scientist|machine learning|deep learning|computer vision|nlp|natural language|ai engineer|ai research|artificial intelligence|quantitative research|data analyst|business intelligence|bi analyst|analytics intern|data analytics|analytics engineering|applied scientist|ai intern)\b"
    ]),
    ("Hardware & Electrical Engineering", [
        r"\b(?:hardware|electrical|electronics|pcb|fpga|asic|rf engineer|semiconductor|vlsi|silicon|circuits|microelectronics|optics|photonics|test hardware)\b"
    ]),
    ("Mechanical & Aerospace Engineering", [
        r"\b(?:mechanical|aerospace|aeronautical|avionics|propulsion|spacecraft|thermal engineer|structural engineer|cad|robotics engineer|mechatronics|manufacturing|tooling|machining|automation engineering|fluid dynamics|materials science|metallurgy)\b"
    ]),
    ("Product Management & Design", [
        r"\b(?:product management|product manager|associate product manager|apm|product owner|ui/ux|ui ux|ux design|ui design|product design|user research|interaction design|graphic design|creative design|industrial design|product development|design intern)\b"
    ]),
    ("Finance, Accounting & Trading", [
        r"\b(?:finance|financial analyst|accounting|accountant|audit|tax|treasury|actuarial|actuary|investment banking|private equity|wealth management|asset management|trader|trading|quantitative trading|portfolio management|credit analyst|risk management|underwriting|real estate.*equity)\b"
    ]),
    ("Marketing & Communications", [
        r"\b(?:marketing|digital marketing|content marketing|social media|brand|growth marketing|public relations|communications|copywriting|seo|sem|event marketing|advertising|market research|pr intern|creative strategy)\b"
    ]),
    ("Sales & Business Development", [
        r"\b(?:sales|business development|bizdev|account executive|account manager|sales operations|sales ops|customer success|client solutions|partnerships|commercial|technical sales)\b"
    ]),
    ("Supply Chain, Logistics & Operations", [
        r"\b(?:supply chain|logistics|procurement|sourcing|operations|ops intern|inventory|warehouse|fulfillment|transportation|freight|production planning|distribution|plant operations|purchasing|quality intern|inspection intern)\b"
    ]),
    ("Healthcare, Biotech & Life Sciences", [
        r"\b(?:clinical|pharmacology|pharmacy|pharmaceutical|biology|biochem|biomedical|chemistry|chemist|lab tech|laboratory|medical|nursing|healthcare|therapeutics|drug discovery|genomics|biotech|veterinary|health)\b"
    ]),
    ("Human Resources & Recruiting", [
        r"\b(?:human resources|hr intern|recruiting|recruiter|talent acquisition|people operations|people ops|diversity|compensation|workforce|employee relations|global talent)\b"
    ]),
    ("Legal, Policy & Compliance", [
        r"\b(?:legal|counsel|paralegal|compliance|regulatory|policy|government affairs|contracts|privacy|ethics|law intern)\b"
    ]),
    ("Civil & Environmental Engineering", [
        r"\b(?:civil|environmental engineer|environmental engineering|structural civil|construction management|geotechnical|surveying|hydrology|water resources|transportation engineer|urban planning|sustainability|mining engineer|ehs intern|environmental intern)\b"
    ]),
    ("General Business & Consulting", [
        r"\b(?:consulting|consultant|management consulting|strategy|business analyst|general business|corporate strategy|operations consulting|project coordinator|project management|pmo|administrative)\b"
    ])
]

_COMPILED_JF_RULES = [(name, [re.compile(p, re.I) for p in pats]) for name, pats in JOB_FUNCTION_RULES]

_JF_CANONICAL_MAP = {
    "software engineering": "Software Engineering",
    "information technology": "Software Engineering",
    "data and analytics": "Data, AI & Machine Learning",
    "data science": "Data, AI & Machine Learning",
    "engineering": "Mechanical & Aerospace Engineering",
    "manufacturing": "Mechanical & Aerospace Engineering",
    "science and engineering": "Healthcare, Biotech & Life Sciences",
    "healthcare": "Healthcare, Biotech & Life Sciences",
    "human resources": "Human Resources & Recruiting",
    "human resources and recruitment": "Human Resources & Recruiting",
    "advertising and marketing": "Marketing & Communications",
    "marketing": "Marketing & Communications",
    "accounting and finance": "Finance, Accounting & Trading",
    "sales": "Sales & Business Development",
    "supply chain": "Supply Chain, Logistics & Operations",
    "business operations": "Supply Chain, Logistics & Operations",
    "management": "General Business & Consulting",
    "consulting": "General Business & Consulting",
    "project management": "General Business & Consulting",
    "general business": "General Business & Consulting",
    "administrative": "General Business & Consulting",
    "product management": "Product Management & Design",
    "design": "Product Management & Design",
    "civil engineering": "Civil & Environmental Engineering"
}


def standardize_job_function(title: str, snippet: str = "", current_jf: str = "") -> str:
    """Classifies a posting into one of the standard job functions."""
    t = title or ""
    for name, pats in _COMPILED_JF_RULES:
        if any(pat.search(t) for pat in pats):
            return name

    if current_jf:
        c_lower = current_jf.strip().lower()
        if c_lower in _JF_CANONICAL_MAP:
            return _JF_CANONICAL_MAP[c_lower]

    if snippet:
        for name, pats in _COMPILED_JF_RULES:
            if any(pat.search(snippet[:300]) for pat in pats):
                return name

    return "Other"


# =====================================================================
# 2. WORK ARRANGEMENT NORMALIZATION
# =====================================================================

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"

_HYBRID_RE = re.compile(r"\b(hybrid)\b", re.I)
_REMOTE_RE = re.compile(r"\b(remote|telework|telecommute|work\s?from\s?home|wfh|virtual|flexible\s*/\s*remote)\b", re.I)
_ONSITE_RE = re.compile(r"\b(on[-\s]?site|in[-\s]?office|in[-\s]?person)\b", re.I)


def standardize_work_arrangement(title: str, location: str, current_wa: str = "") -> str:
    """Normalizes work arrangement into 'remote', 'hybrid', 'onsite', or ''."""
    wa = (current_wa or "").strip().lower()
    if wa in (REMOTE, HYBRID, ONSITE):
        return wa

    combined = f"{title or ''} | {location or ''}"
    if _HYBRID_RE.search(combined):
        return HYBRID
    if _REMOTE_RE.search(combined):
        return REMOTE
    if _ONSITE_RE.search(combined):
        return ONSITE
    return ""


# =====================================================================
# 3. LOCATION STANDARDIZATION
# =====================================================================

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
    'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
    'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
    'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC'
}

STATE_CODES = set(US_STATES.values())

MAJOR_METROS = {
    'san francisco': 'San Francisco, CA',
    'new york': 'New York, NY',
    'new york city': 'New York, NY',
    'chicago': 'Chicago, IL',
    'chicago il': 'Chicago, IL',
    'seattle': 'Seattle, WA',
    'austin': 'Austin, TX',
    'boston': 'Boston, MA',
    'los angeles': 'Los Angeles, CA',
    'san jose': 'San Jose, CA',
    'palo alto': 'Palo Alto, CA',
    'mountain view': 'Mountain View, CA',
    'sunnyvale': 'Sunnyvale, CA',
    'santa clara': 'Santa Clara, CA',
    'atlanta': 'Atlanta, GA',
    'dallas': 'Dallas, TX',
    'houston': 'Houston, TX',
    'san diego': 'San Diego, CA',
    'denver': 'Denver, CO',
    'washington': 'Washington, DC',
    'washington dc': 'Washington, DC',
    'washington, d.c.': 'Washington, DC',
    'munich': 'Munich, Germany',
    'münchen': 'Munich, Germany',
    'berlin hq': 'Berlin, Germany',
    'hong kong': 'Hong Kong',
    'shanghai': 'Shanghai, China'
}

KNOWN_INTERNATIONAL_CITIES = {
    'singapore': 'Singapore', 'singapore, sg': 'Singapore',
    'london': 'London, United Kingdom', 'london, uk': 'London, United Kingdom',
    'paris': 'Paris, France', 'berlin': 'Berlin, Germany',
    'tokyo': 'Tokyo, Japan', 'madrid': 'Madrid, Spain',
    'sydney': 'Sydney, Australia', 'toronto': 'Toronto, Canada',
    'toronto, on': 'Toronto, ON, Canada', 'vancouver': 'Vancouver, Canada',
    'vancouver, bc': 'Vancouver, BC, Canada', 'montreal': 'Montreal, Canada',
    'montreal, qc': 'Montreal, QC, Canada', 'auckland': 'Auckland, New Zealand',
    'auckland, nz': 'Auckland, New Zealand', 'dublin': 'Dublin, Ireland',
    'amsterdam': 'Amsterdam, Netherlands', 'seoul': 'Seoul, South Korea',
    'bangkok': 'Bangkok, Thailand', 'jakarta': 'Jakarta, Indonesia',
    'kuala lumpur': 'Kuala Lumpur, Malaysia', 'são paulo': 'São Paulo, Brazil',
    'sao paulo': 'São Paulo, Brazil', 'ho chi minh city': 'Ho Chi Minh City, Vietnam',
    'mexico city': 'Mexico City, Mexico', 'bengaluru': 'Bengaluru, India',
    'bangalore': 'Bengaluru, India', 'hyderabad': 'Hyderabad, India',
    'mumbai': 'Mumbai, India', 'pune': 'Pune, India'
}

ISO3_COUNTRY_MAP = {
    'sgp': 'Singapore', 'sg': 'Singapore',
    'chl': 'Chile', 'cl': 'Chile',
    'mex': 'Mexico', 'mx': 'Mexico',
    'mys': 'Malaysia', 'my': 'Malaysia',
    'ita': 'Italy', 'it': 'Italy',
    'pol': 'Poland', 'pl': 'Poland',
    'pan': 'Panama', 'pa': 'Panama',
    'per': 'Peru', 'pe': 'Peru',
    'gbr': 'United Kingdom', 'gb': 'United Kingdom', 'uk': 'United Kingdom',
    'deu': 'Germany', 'de': 'Germany',
    'fra': 'France', 'fr': 'France',
    'esp': 'Spain', 'es': 'Spain',
    'aus': 'Australia', 'au': 'Australia',
    'can': 'Canada', 'ca': 'Canada',
    'bra': 'Brazil', 'br': 'Brazil',
    'ind': 'India', 'in': 'India'
}

INVALID_LOC_STRINGS = {
    'in-office', 'headquarter', 'headquarters', 'nea headquarters',
    'office', 'flexible - any spacex site', 'united states'
}


def standardize_location(raw_loc: str) -> str:
    """Cleans up raw ATS location strings into standardized display strings."""
    if not raw_loc or not raw_loc.strip():
        return "Not Specified"

    loc = raw_loc.strip().replace("\xa0", " ")

    if loc.lower().strip() in INVALID_LOC_STRINGS:
        return "Not Specified"

    # 1. Multi-location counts or multi-city lists (separated by ; or | or multiple US states)
    if re.search(r"\b\d+\s+Locations?\b|\bMultiple\s+Locations?\b", loc, re.I):
        return "Multiple Locations"

    if ";" in loc or " | " in loc:
        # Check if multiple locations are chained
        parts = [p.strip() for p in re.split(r"[;|]", loc) if p.strip()]
        if len(parts) >= 2:
            return "Multiple Locations"

    # 2. Remote / Virtual variations
    clean_r = re.sub(r"^[\s\-|:/]+|[\s\-|:/]+$", "", loc).strip()
    if re.match(r"^(?:remote|virtual|flexible\s*/\s*remote)(?:\s*-\s*(?:us|usa|united states|united states of america))?$", clean_r, re.I) or re.match(r"^(?:us|usa|united states)\s*-\s*remote$", clean_r, re.I):
        return "Remote, United States"

    # Strip Remote / Hybrid tag prefixes or suffixes (e.g. "Remote - Los Angeles, CA", "Toronto, Canada (Hybrid)")
    loc = re.sub(r"^(?:remote|virtual|hybrid|flexible\s*/\s*remote)\s*-\s*", "", loc, flags=re.I)
    loc = re.sub(r"\s*\((?:remote|hybrid|onsite|virtual)\)$", "", loc, flags=re.I)
    loc = re.sub(r"[,\s]+(?:remote|virtual|hybrid)$", "", loc, flags=re.I)

    # 3. Clean building / address codes (e.g. CA-QC-LONGUEUIL-J01 ~ 1000 Blvd ...)
    if "~" in loc:
        prefix = loc.split("~")[0].strip()
        m = re.match(r"^(?:CA|US|USA)-([A-Z]{2})-([A-Z\s]+)(?:-\w+)?$", prefix)
        if m:
            st, city = m.group(1), m.group(2).title()
            return f"{city}, {st}"
        loc = prefix

    # 4. Workday ISO-3 / country prefix format: "SGP - Woodlands", "MYS - Penang", "MEX-Queretaro-Queretaro", "CHL - Region - Santiago"
    if "-" in loc:
        dash_parts = [p.strip() for p in loc.split("-") if p.strip()]
        if len(dash_parts) >= 2:
            c_code = dash_parts[0].lower()
            if c_code in ISO3_COUNTRY_MAP:
                country_name = ISO3_COUNTRY_MAP[c_code]
                if country_name == "Singapore":
                    return "Singapore"
                city_part = dash_parts[-1]
                return f"{city_part.title()}, {country_name}"

    # Workday prefix format: "USA - Ohio - Columbus" or "US - CA - San Francisco"
    m = re.match(r"^(?:USA?|United States)\s*-\s*([A-Za-z\s]+)\s*-\s*(.+)$", loc, re.I)
    if m:
        state_raw, city_raw = m.group(1).strip(), m.group(2).strip()
        state_code = US_STATES.get(state_raw.lower(), state_raw.upper() if len(state_raw) == 2 else state_raw)
        return f"{city_raw}, {state_code}"

    # 5. Clean leading street addresses (e.g. "1100 Crown Colony Drive, Quincy, MA 02169" -> "Quincy, MA")
    loc = re.sub(r"^\d+\s+[A-Za-z0-9\.\s]+(?:Street|St|Avenue|Ave|Drive|Dr|Road|Rd|Boulevard|Blvd|Way|Lane|Ln|Place|Pl|Court|Ct|Parkway|Pkwy|Pike|Highway|Hwy|Floor|Fl|Suite|Ste)\s*,\s*", "", loc, flags=re.I)

    # 6. Clean trailing ZIP codes & country names (e.g. "Washington, DC 20004" -> "Washington, DC")
    loc = re.sub(r"[,\s]+\d{5}(?:-\d{4})?\s*$", "", loc)
    loc = re.sub(r"[,\s]+(?:USA|United States of America|United States|US)\s*$", "", loc, flags=re.I)

    # 7. Check Major Metros direct mapping
    loc_lower = loc.strip().lower()
    if loc_lower in MAJOR_METROS:
        return MAJOR_METROS[loc_lower]

    # 8. Check known international cities
    if loc_lower in KNOWN_INTERNATIONAL_CITIES:
        return KNOWN_INTERNATIONAL_CITIES[loc_lower]

    # 9. Normalize "City, StateName" to "City, ST"
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) == 2:
        city, st = parts[0], parts[1]
        st_clean = re.sub(r"\s*\d{5}(?:-\d{4})?$", "", st).strip()
        if st_clean.lower() in US_STATES:
            return f"{city}, {US_STATES[st_clean.lower()]}"
        elif st_clean.upper() in STATE_CODES:
            return f"{city}, {st_clean.upper()}"

    # 10. Collapse extra spaces
    loc = re.sub(r"\s+", " ", loc).strip()
    return loc


# =====================================================================
# 4. CYCLE EXTRACTION
# =====================================================================

SEASON_PATTERNS = [
    ("summer", r"\b(?:summer|été|ete|verano|sommer|estate)\b"),
    ("fall", r"\b(?:fall|autumn|automne|otoño|herbst|autunno)\b"),
    ("winter", r"\b(?:winter|hiver|invierno|inverno)\b"),
    ("spring", r"\b(?:spring|printemps|primavera|frühling)\b"),
]

YEAR_PATTERNS = [
    r"\b(202[4-9]|203[0-5])\b",
    r"\b(?:FY|fy)\s*'?([2-3][0-9])\b",
    r"(?<![\w])'([2-3][0-9])\b"
]

_COMPILED_SEASONS = [(name, re.compile(p, re.I)) for name, p in SEASON_PATTERNS]
_COMPILED_YEARS = [re.compile(p) for p in YEAR_PATTERNS]


def standardize_cycle(title: str, snippet: str = "", current_season: str = "", current_year: Optional[int] = None) -> tuple[str, Optional[int]]:
    """Extracts (season, year) from title and snippet if missing."""
    t = title or ""
    s = snippet or ""

    season = current_season or ""
    if not season:
        for name, pat in _COMPILED_SEASONS:
            if pat.search(t):
                season = name
                break
        if not season and s:
            for name, pat in _COMPILED_SEASONS:
                if pat.search(s[:150]):
                    season = name
                    break

    year = current_year
    if year is None:
        for i, pat in enumerate(_COMPILED_YEARS):
            m = pat.search(t)
            if m:
                year = int(m.group(1)) if i == 0 else 2000 + int(m.group(1))
                break
        if year is None and s:
            for i, pat in enumerate(_COMPILED_YEARS):
                m = pat.search(s[:150])
                if m:
                    year = int(m.group(1)) if i == 0 else 2000 + int(m.group(1))
                    break

    return season, year


# =====================================================================
# 5. TITLE CLEANING
# =====================================================================

_REQ_PATTERNS = [
    re.compile(r"\s*[\[\(]?(?:req(?:uisition)?|job\s*id|ref(?:erence)?|req\s*#|req\s*no\.?)\s*[:#-]?\s*[a-z0-9_-]+[\]\)]?", re.I),
    re.compile(r"\s*\b(?:JR\d{4,}|R-\d{4,}|#\d{4,})\b", re.I)
]


def clean_display_title(raw_title: str) -> str:
    """Cleans requisition IDs, bilingual split tags, and all-caps noise."""
    if not raw_title:
        return ""
    t = raw_title.replace("\xa0", " ").strip()

    # 1. Strip requisition codes
    for pat in _REQ_PATTERNS:
        t = pat.sub("", t)

    # 2. Bilingual splits (e.g. French / English separated by " / " or " | ")
    if " / Internship" in t or " | Internship" in t or " / Intern" in t or " | Intern" in t:
        parts = re.split(r"\s*[/|]\s*(?=Intern)", t, flags=re.I)
        if len(parts) > 1:
            t = parts[-1].strip()

    # 3. ALL CAPS to Title Case
    if t.isupper() and len(t) > 4:
        words = t.split()
        acronyms = {"AI", "ML", "IT", "QA", "SWE", "RF", "PCB", "EHS", "HR", "FPGA", "ASIC", "VLSI", "CAD", "CAE", "UI", "UX", "API", "OS", "USA", "UK"}
        c_words = [w.upper() if w.upper() in acronyms else w.capitalize() for w in words]
        t = " ".join(c_words)

    # 4. Clean leading/trailing punctuation & collapse spaces
    t = re.sub(r"^[\s\-|:/]+|[\s\-|:/]+$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# =====================================================================
# 6. COMPANY DISPLAY NAME RESOLUTION
# =====================================================================

COMPANY_EXACT_MAP = {
    'cvshealth': 'CVS Health',
    'aecom2': 'AECOM',
    'pimco': 'PIMCO',
    'globalhr': 'Pratt & Whitney',
    'pg': 'Procter & Gamble (P&G)',
    'goventi': 'Goventi',
    'etched': 'Etched',
    'abbvie': 'AbbVie',
    'conocophillips': 'ConocoPhillips',
    'southstatebank': 'SouthState Bank',
    'valeo': 'Valeo',
    'sluhn': "St. Luke's University Health Network",
    'siloamcareers': 'Siloam Hospitals',
    'generalmotors': 'General Motors',
    'enpal': 'Enpal',
    'simular': 'Simular',
    'brunswick': 'Brunswick Group',
    'nvidia': 'NVIDIA',
    'clera': 'Clera',
    'nea': 'New Enterprise Associates (NEA)',
    'joko': 'Joko',
    'philips': 'Philips',
    'prosidianconsulting': 'ProSidian Consulting',
    'ri': 'Research Institute',
    'voodoo': 'Voodoo',
    'samsara inc.': 'Samsara',
    'aptiv plc': 'Aptiv',
    'hp': 'HP Inc.',
    'hp inc.': 'HP Inc.',
    'itw': 'ITW (Illinois Tool Works)',
    'csl': 'CSL',
    'ecovadis': 'EcoVadis',
    'cityofphiladelphia': 'City of Philadelphia',
    'ge': 'GE (General Electric)',
    'ups': 'UPS',
    'cibc': 'CIBC',
    'td': 'TD Bank',
    'bmo': 'BMO Financial Group',
    'rbc': 'RBC (Royal Bank of Canada)',
    'ey': 'EY (Ernst & Young)',
    'pwc': 'PwC',
    'kpmg': 'KPMG',
    'bain': 'Bain & Company',
    'bcg': 'Boston Consulting Group (BCG)',
    'mckinsey': 'McKinsey & Company',
    'jpmorgan': 'JPMorgan Chase',
    'goldmansachs': 'Goldman Sachs',
    'ms': 'Morgan Stanley',
    'citi': 'Citigroup',
    'bofa': 'Bank of America',
    'amzn': 'Amazon',
    'goog': 'Google',
    'meta': 'Meta',
    'aapl': 'Apple',
    'msft': 'Microsoft',
    'tsla': 'Tesla',
    'neuralink corp.': 'Neuralink',
    'hntb corporation': 'HNTB',
    'invesco ltd.': 'Invesco',
    'polaris inc.': 'Polaris',
    'ciena corporation': 'Ciena',
    'equinix, inc': 'Equinix',
    'block, inc.': 'Block (Square)',
    'gartner, inc.': 'Gartner',
    'comcast corporation': 'Comcast',
    'chevron corporation': 'Chevron',
    'hootsuite inc.': 'Hootsuite',
    'epicor software corporation': 'Epicor',
    'genscript biotech corporation': 'GenScript Biotech',
    'point72': 'Point72',
    'project44': 'project44',
    'auto1': 'AUTO1 Group',
    'autoscout24': 'AutoScout24',
    'rise8': 'Rise8',
    '10beauty': '10Beauty',
    '42dot': '42dot',
    '66degrees': '66degrees'
}

_CAMEL_CASE_SPLITS = [
    (r"\bCityAndCountyOfSanFrancisco\b", "City and County of San Francisco"),
    (r"\bWorldWildlifeFundInc\b", "World Wildlife Fund"),
    (r"\bBEUMERGroup\b", "BEUMER Group"),
    (r"\bAmrefHealthAfrica\b", "Amref Health Africa"),
    (r"\bSigmaSoftware\b", "Sigma Software"),
    (r"\bRAKUNA\b", "Rakuna")
]


def standardize_company_name(raw_name: str) -> str:
    """Cleans up raw lowercase slugs, tenant instance numbers, and legal clutter."""
    if not raw_name:
        return "Unknown Company"
    name = raw_name.strip()
    n_lower = name.lower()

    # 1. Exact canonical matches
    if n_lower in COMPANY_EXACT_MAP:
        return COMPANY_EXACT_MAP[n_lower]

    # 2. Subsidiary prefix codes (e.g. "11115 Expedia do Brasil ...", "1200 MyPath ...")
    if re.match(r"^\d{4,5}\s+Expedia\b", name, re.I):
        return "Expedia Group"
    if re.match(r"^\d{4,5}\s+MyPath\b", name, re.I):
        return "MyPath"

    # 3. Strip trailing ATS tenant instance numbers (e.g. NBCUniversal3 -> NBCUniversal, Ubisoft2 -> Ubisoft)
    name = re.sub(r"(?<=[a-zA-Z])\d+$", "", name)

    # 4. Known CamelCase replacements
    for pat, repl in _CAMEL_CASE_SPLITS:
        name = re.sub(pat, repl, name, flags=re.I)

    # 5. Strip legal entity suffixes (Inc, LLC, Corp, Ltd, etc.) except for short/branded names
    if name not in ("HP Inc.", "Moog Inc.", "Dow Inc.", "CSL Limited", "CVS Health"):
        name = re.sub(r"[,\s]+(?:Inc\.?|LLC\.?|Corp\.?|Corporation|Ltd\.?|Limited|GmbH|PLC|Pty|S\.?A\.?|N\.?V\.?|B\.?V\.?)\s*$", "", name, flags=re.I)

    # 6. If all lowercase and no spaces, title case it
    if name == name.lower() and " " not in name and len(name) > 2:
        return name.title()

    return name.strip()



# =====================================================================
# 7. COMPANY INDUSTRY CATEGORY MAPPING
# =====================================================================

COMPANY_CATEGORY_MAP = {
    'Pratt & Whitney': 'Aerospace & Defense',
    'RTX': 'Aerospace & Defense',
    'GE Aerospace': 'Aerospace & Defense',
    'Rocket Lab': 'Space Launch & Satellites',
    'Zipline': 'Drone Delivery',
    'TikTok': 'Digital Media & Entertainment',
    'Walmart': 'Retail & E-Commerce',
    'BoschGroup': 'Industrial & Automotive Technology',
    'Bosch': 'Industrial & Automotive Technology',
    'Enterprise Mobility': 'Transportation & Mobility',
    'Schneider Electric': 'Power & Electrical Equipment',
    'CVS Health': 'Healthcare & Pharmacy',
    'Aumovio': 'Automotive Technology',
    'Continental': 'Automotive',
    'AECOM': 'Civil Engineering & Infrastructure',
    'DeliveryHero': 'Food Delivery & Logistics',
    'GE Vernova': 'Energy & Power',
    'Hitachi Energy': 'Energy & Power',
    'JPMorgan Chase': 'Banking & Financial Services',
    'PIMCO': 'Asset Management & Finance',
    'PNC': 'Banking & Financial Services',
    'Blackstone': 'Private Equity & Asset Management',
    'AccorHotel': 'Hospitality & Travel',
    'Selina': 'Hospitality & Travel',
    'RedBull': 'Beverages & Sports Marketing',
    'Eurofins': 'Life Sciences & Testing',
    'WesternDigital': 'Computer Hardware & Storage',
    'SGS': 'Testing, Inspection & Certification',
    'Equinox': 'Fitness & Hospitality',
    'Procter & Gamble (P&G)': 'Consumer Packaged Goods',
    'Sia': 'Management Consulting',
    'Palantir': 'Enterprise Software',
    'NVIDIA': 'Semiconductors & AI Hardware',
    'General Motors': 'Automotive',
    'AbbVie': 'Pharmaceuticals & Biotech',
    'ConocoPhillips': 'Oil & Gas',
    'Tesla': 'Electric Vehicles & Clean Energy',
    'Apple': 'Consumer Electronics & Software',
    'Microsoft': 'Cloud & Enterprise Software',
    'Amazon': 'E-Commerce & Cloud Infrastructure',
    'Google': 'Internet & AI Software',
    'Meta': 'Social Media & AI Software'
}


def infer_category(company: str, title: str, job_function: str, current_category: str) -> str:
    """Intelligently classifies a posting into an industry/domain sector without dropping any."""
    if current_category and current_category.strip() and current_category != "Uncategorized":
        return current_category.strip()

    # 1. Exact company mappings
    if company in COMPANY_CATEGORY_MAP:
        return COMPANY_CATEGORY_MAP[company]

    # 2. Company name heuristic rules
    c_lower = (company or "").lower()
    if re.search(r"\b(?:hospital|health|clinic|medical|pharma|therapeutics|biosciences|biotech)\b", c_lower):
        return "Healthcare & Life Sciences"
    if re.search(r"\b(?:bank|capital|financial|investment|wealth|securities|insurance|fintech|bancorp)\b", c_lower):
        return "Financial Services"
    if re.search(r"\b(?:aerospace|aero|aviation|defense|space)\b", c_lower):
        return "Aerospace & Defense"
    if re.search(r"\b(?:energy|power|solar|renewable|petroleum|oil|gas)\b", c_lower):
        return "Energy & Utilities"
    if re.search(r"\b(?:auto|motors|automotive|vehicles)\b", c_lower):
        return "Automotive"
    if re.search(r"\b(?:hotel|hospitality|resort|travel|airlines|airways)\b", c_lower):
        return "Hospitality & Travel"
    if re.search(r"\b(?:retail|brands|foods|beverage|apparel|stores)\b", c_lower):
        return "Consumer Goods & Retail"
    if re.search(r"\b(?:construction|civil|infrastructure)\b", c_lower):
        return "Civil Engineering & Construction"
    if re.search(r"\b(?:media|entertainment|broadcasting|publishing|news)\b", c_lower):
        return "Media & Entertainment"

    # 3. Derive from classified job function
    if job_function:
        fn_map = {
            "Software Engineering": "Software & Technology",
            "Data, AI & Machine Learning": "Data & Artificial Intelligence",
            "Hardware & Electrical Engineering": "Hardware & Electronics",
            "Mechanical & Aerospace Engineering": "Mechanical Engineering",
            "Civil, Structural & Environmental Engineering": "Civil & Environmental Engineering",
            "Product & Program Management": "Product Management",
            "Finance, Accounting & Trading": "Finance & Accounting",
            "Marketing, Brand & Growth": "Marketing & Growth",
            "Sales, Account Management & BD": "Sales & Business Development",
            "Supply Chain, Logistics & Operations": "Supply Chain & Logistics",
            "Healthcare & Medicine": "Healthcare & Life Sciences",
            "Human Resources & Talent": "Human Resources & Talent",
            "Legal, Compliance & Policy": "Legal & Compliance",
            "Strategy, Consulting & Corporate Dev": "Management Consulting"
        }
        if job_function in fn_map:
            return fn_map[job_function]

    # 4. Fallback: if nothing matches, preserve as Uncategorized (NEVER drop)
    return current_category or "Uncategorized"

# =====================================================================
# 8. UNIFIED POSTING STANDARDIZATION
# =====================================================================

def standardize_posting(p: dict) -> dict:
    """Enriches and standardizes a single posting dictionary."""
    d = dict(p)

    raw_title = d.get("title", "")
    d["title"] = clean_display_title(raw_title)

    raw_loc = d.get("location", "")
    d["location"] = standardize_location(raw_loc)

    d["work_arrangement"] = standardize_work_arrangement(raw_title, raw_loc, d.get("work_arrangement", ""))

    d["job_function"] = standardize_job_function(raw_title, d.get("description_snippet", ""), d.get("job_function", ""))

    season, year = standardize_cycle(raw_title, d.get("description_snippet", ""), d.get("cycle_season", ""), d.get("cycle_year"))
    d["cycle_season"] = season
    d["cycle_year"] = year

    # Special handling for Workday tenant subdomains like globalhr -> Pratt & Whitney
    url = d.get("url", "") or ""
    c_raw = d.get("company", "") or ""
    ck_raw = d.get("company_key", "") or ""
    if "globalhr.wd5.myworkdayjobs.com" in url or c_raw.lower() == "globalhr" or ck_raw.lower() == "globalhr":
        d["company"] = "Pratt & Whitney"
        d["company_key"] = "prattwhitney"
        d["category"] = "Aerospace & Defense"
    else:
        d["company"] = standardize_company_name(c_raw)

    # Intelligently infer category while preserving Uncategorized fallbacks (never drop)
    d["category"] = infer_category(d["company"], d["title"], d["job_function"], d.get("category", ""))

    return d