"""Tests for standardization engine (service/standardize.py)"""

from service.standardize import (
    standardize_job_function,
    standardize_work_arrangement,
    standardize_location,
    standardize_cycle,
    clean_display_title,
    standardize_company_name,
    standardize_posting
)


def test_standardize_job_function():
    assert standardize_job_function("Software Engineer Intern") == "Software Engineering"
    assert standardize_job_function("Frontend Developer Co-op") == "Software Engineering"
    assert standardize_job_function("Data Scientist Intern (Summer 2026)") == "Data, AI & Machine Learning"
    assert standardize_job_function("AI Research Engineer Intern") == "Data, AI & Machine Learning"
    assert standardize_job_function("Hardware Engineering Intern") == "Hardware & Electrical Engineering"
    assert standardize_job_function("Electrical & Electronics Co-op") == "Hardware & Electrical Engineering"
    assert standardize_job_function("Mechanical Engineer Intern") == "Mechanical & Aerospace Engineering"
    assert standardize_job_function("Aerospace Propulsion Co-op") == "Mechanical & Aerospace Engineering"
    assert standardize_job_function("Associate Product Manager Intern (APM)") == "Product Management & Design"
    assert standardize_job_function("UI/UX Product Design Intern") == "Product Management & Design"
    assert standardize_job_function("Financial Analyst Intern") == "Finance, Accounting & Trading"
    assert standardize_job_function("Investment Banking Summer Analyst") == "Finance, Accounting & Trading"
    assert standardize_job_function("Growth Marketing Intern") == "Marketing & Communications"
    assert standardize_job_function("Sales Operations Intern") == "Sales & Business Development"
    assert standardize_job_function("Supply Chain & Logistics Intern") == "Supply Chain, Logistics & Operations"
    assert standardize_job_function("Clinical Pharmacology Intern") == "Healthcare, Biotech & Life Sciences"
    assert standardize_job_function("Talent Acquisition & HR Intern") == "Human Resources & Recruiting"
    assert standardize_job_function("Legal & Regulatory Compliance Intern") == "Legal, Policy & Compliance"
    assert standardize_job_function("Civil Engineering Co-op") == "Civil & Environmental Engineering"
    assert standardize_job_function("Management Consulting Summer Associate") == "General Business & Consulting"


def test_standardize_work_arrangement():
    assert standardize_work_arrangement("Software Engineer Intern [Remote]", "San Francisco, CA") == "remote"
    assert standardize_work_arrangement("Product Design Intern (Hybrid)", "New York, NY") == "hybrid"
    assert standardize_work_arrangement("Hardware Engineering Co-op", "Austin, TX (Onsite)") == "onsite"
    assert standardize_work_arrangement("Data Analyst Intern", "Remote, United States") == "remote"
    assert standardize_work_arrangement("Mechanical Engineer Intern", "Chicago, IL") == ""


def test_standardize_location():
    # US States & Postal Codes
    assert standardize_location("San Francisco, California, USA") == "San Francisco, CA"
    assert standardize_location("New York, New York") == "New York, NY"
    assert standardize_location("Austin, Texas") == "Austin, TX"
    assert standardize_location("Washington, DC 20004") == "Washington, DC"
    assert standardize_location("1100 Crown Colony Drive, Quincy, MA 02169") == "Quincy, MA"
    assert standardize_location("510 East 62nd Street, New York, NY 10065") == "New York, NY"
    assert standardize_location("USA - Ohio - Columbus") == "Columbus, OH"
    assert standardize_location("US - CA - San Francisco") == "San Francisco, CA"
    
    # Major Metros & Bare Cities
    assert standardize_location("San Francisco") == "San Francisco, CA"
    assert standardize_location("New York City") == "New York, NY"
    assert standardize_location("Chicago") == "Chicago, IL"
    assert standardize_location("Seattle") == "Seattle, WA"
    
    # Multi-Location
    assert standardize_location("12 Locations") == "Multiple Locations"
    assert standardize_location("Austin, TX; New York, NY") == "Multiple Locations"
    assert standardize_location("Barcelona; Berlin; London; Munich; Paris") == "Multiple Locations"
    
    # International & ATS Country Prefixes
    assert standardize_location("SGP - Woodlands") == "Singapore"
    assert standardize_location("MYS - Penang") == "Penang, Malaysia"
    assert standardize_location("CHL - Region Metropolitana de Santiago - Santiago") == "Santiago, Chile"
    assert standardize_location("London, UK") == "London, United Kingdom"
    assert standardize_location("Singapore, SG") == "Singapore"
    assert standardize_location("Toronto, ON") == "Toronto, ON, Canada"
    
    # Remote variations
    assert standardize_location("Remote - US") == "Remote, United States"
    assert standardize_location("Remote - Los Angeles, CA") == "Los Angeles, CA"
    assert standardize_location("Toronto, Canada (Hybrid)") == "Toronto, Canada"
    assert standardize_location("In-Office") == "Not Specified"


def test_standardize_cycle():
    assert standardize_cycle("Software Engineer Intern - Summer 2026") == ("summer", 2026)
    assert standardize_cycle("Data Analyst Co-op (Fall '25)") == ("fall", 2025)
    assert standardize_cycle("Stagiaire Hiver 2026") == ("winter", 2026)
    assert standardize_cycle("Pasante de Verano 2027") == ("summer", 2027)
    assert standardize_cycle("Spring 2026 Intern", current_season="fall") == ("fall", 2026)


def test_clean_display_title():
    assert clean_display_title("Software Engineer Intern (REQ-12345)") == "Software Engineer Intern"
    assert clean_display_title("Data Science Intern - JR001923") == "Data Science Intern"
    assert clean_display_title("SOFTWARE ENGINEER INTERN") == "Software Engineer Intern"
    assert clean_display_title("AI / ML RESEARCH INTERN") == "AI / ML Research Intern"
    assert clean_display_title("Stage Ingénieur / Internship Software Engineering") == "Internship Software Engineering"


def test_standardize_company_name():
    # Slugs
    assert standardize_company_name("cvshealth") == "CVS Health"
    assert standardize_company_name("globalhr") == "Pratt & Whitney"
    assert standardize_company_name("nvidia") == "NVIDIA"
    assert standardize_company_name("generalmotors") == "General Motors"
    
    # Tenant instance numbers
    assert standardize_company_name("NBCUniversal3") == "NBCUniversal"
    assert standardize_company_name("Ubisoft2") == "Ubisoft"
    assert standardize_company_name("Miratech1") == "Miratech"
    assert standardize_company_name("Wavestone1") == "Wavestone"
    
    # Legal Entity Suffixes
    assert standardize_company_name("HNTB Corporation") == "HNTB"
    assert standardize_company_name("Invesco Ltd.") == "Invesco"
    assert standardize_company_name("Polaris Inc.") == "Polaris"
    assert standardize_company_name("HP Inc.") == "HP Inc."
    
    # CamelCase splits
    assert standardize_company_name("CityAndCountyOfSanFrancisco1") == "City and County of San Francisco"
    assert standardize_company_name("WorldWildlifeFundInc1") == "World Wildlife Fund"


def test_standardize_posting():
    raw = {
        "id": "abc123",
        "company": "cvshealth",
        "title": "SOFTWARE ENGINEER INTERN (REQ-999)",
        "location": "San Francisco, California, USA",
        "description_snippet": "Summer 2026 internship in software engineering",
        "category": "Health Insurance"
    }
    std = standardize_posting(raw)
    assert std["company"] == "CVS Health"
    assert std["title"] == "Software Engineer Intern"
    assert std["location"] == "San Francisco, CA"
    assert std["job_function"] == "Software Engineering"
    assert std["cycle_season"] == "summer"
    assert std["cycle_year"] == 2026
