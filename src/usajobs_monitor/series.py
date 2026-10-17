"""Common federal occupational series for the setup wizard's picker.

Titles follow OPM's Handbook of Occupational Groups and Families. This is a
short list of frequently posted white-collar series, not the full handbook:
any 4-digit series code works in a config.
https://www.opm.gov/policy-data-oversight/classification-qualifications/classifying-general-schedule-positions/
"""

from __future__ import annotations

import re

COMMON_SERIES = {
    "0018": "Safety and Occupational Health Management",
    "0028": "Environmental Protection Specialist",
    "0080": "Security Administration",
    "0083": "Police",
    "0089": "Emergency Management",
    "0101": "Social Science",
    "0110": "Economist",
    "0130": "Foreign Affairs",
    "0132": "Intelligence",
    "0180": "Psychology",
    "0185": "Social Work",
    "0201": "Human Resources Management",
    "0203": "Human Resources Assistance",
    "0260": "Equal Employment Opportunity",
    "0301": "Miscellaneous Administration and Program",
    "0303": "Miscellaneous Clerk and Assistant",
    "0318": "Secretary",
    "0326": "Office Automation Clerical and Assistance",
    "0340": "Program Management",
    "0341": "Administrative Officer",
    "0343": "Management and Program Analysis",
    "0344": "Management and Program Clerical and Assistance",
    "0346": "Logistics Management",
    "0401": "General Natural Resources Management and Biological Sciences",
    "0460": "Forestry",
    "0486": "Wildlife Biology",
    "0501": "Financial Administration and Program",
    "0503": "Financial Clerical and Technician",
    "0505": "Financial Management",
    "0510": "Accounting",
    "0511": "Auditing",
    "0525": "Accounting Technician",
    "0560": "Budget Analysis",
    "0570": "Financial Institution Examining",
    "0602": "Medical Officer",
    "0610": "Nurse",
    "0660": "Pharmacist",
    "0670": "Health System Administration",
    "0671": "Health System Specialist",
    "0685": "Public Health Program Specialist",
    "0701": "Veterinary Medical Science",
    "0801": "General Engineering",
    "0802": "Engineering Technical",
    "0810": "Civil Engineering",
    "0830": "Mechanical Engineering",
    "0850": "Electrical Engineering",
    "0854": "Computer Engineering",
    "0855": "Electronics Engineering",
    "0861": "Aerospace Engineering",
    "0901": "General Legal and Kindred Administration",
    "0905": "General Attorney",
    "0950": "Paralegal Specialist",
    "0962": "Contact Representative",
    "1035": "Public Affairs",
    "1082": "Writing and Editing",
    "1083": "Technical Writing and Editing",
    "1101": "General Business and Industry",
    "1102": "Contracting",
    "1105": "Purchasing",
    "1150": "Industrial Specialist",
    "1160": "Financial Analysis",
    "1165": "Loan Specialist",
    "1170": "Realty",
    "1301": "General Physical Science",
    "1310": "Physics",
    "1320": "Chemistry",
    "1350": "Geology",
    "1410": "Librarian",
    "1515": "Operations Research",
    "1529": "Mathematical Statistics",
    "1530": "Statistics",
    "1550": "Computer Science",
    "1560": "Data Science",
    "1701": "General Education and Training",
    "1712": "Training Instruction",
    "1750": "Instructional Systems",
    "1801": "General Inspection, Investigation, Enforcement, and Compliance",
    "1811": "Criminal Investigation",
    "1895": "Customs and Border Protection",
    "1910": "Quality Assurance",
    "2001": "General Supply",
    "2003": "Supply Program Management",
    "2010": "Inventory Management",
    "2101": "Transportation Specialist",
    "2152": "Air Traffic Control",
    "2181": "Aircraft Operation",
    "2210": "Information Technology Management",
}


def find_series(query: str) -> list[tuple[str, str]]:
    """Series whose 4-digit code is ``query`` or whose title has every word of it
    (by its first five letters, so "finance" finds "Financial Management")."""
    query = query.strip()
    if re.fullmatch(r"\d{4}", query):
        return [(query, COMMON_SERIES.get(query, "(not in the common list; kept as entered)"))]
    words = re.findall(r"[a-z]+", query.lower())
    if not words:
        return []
    return [(code, title) for code, title in COMMON_SERIES.items()
            if all(re.search(rf"\b{re.escape(w[:5])}", title.lower()) for w in words)]
