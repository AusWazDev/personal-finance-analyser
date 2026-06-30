"""Shared utilities used across reporter.py and recommendations.py."""

import re

# Canonical category → chart colour mapping.
# Add new categories here; reporter.py and server.py both import from this module.
CATEGORY_COLORS: dict[str, str] = {
    "Groceries": "#2A9D8F",
    "Dining Out": "#E9C46A",
    "Housing": "#E63946",
    "Transport": "#264653",
    "Subscriptions": "#F4A261",
    "Utilities": "#457B9D",
    "Health": "#A8DADC",
    "Insurance": "#4CC9F0",
    "Entertainment": "#9B5DE5",
    "Personal Care": "#F15BB5",
    "Travel": "#06D6A0",
    "Education": "#4361EE",
    "Gifts Given": "#F72585",
    "Gifts Received": "#FF70A6",
    "Donations": "#C77DFF",
    "Bank Fees & Charges": "#9D0208",
    "Bank Interest Charged": "#C1121F",
    "Interest Income": "#40916C",
    "Business Reimbursement": "#52B788",
    "Business Expense": "#FB5607",
    "Investment": "#3A86FF",
    "Income": "#00BB77",
    "Board & Lodging": "#4895EF",
    "Family Loan Received": "#4CC9F0",
    "Family Loan Repayment": "#FB8500",
    "Transfers": "#CCCCCC",
    "Miscellaneous": "#8D8D8D",
}

# Single source of truth for the data tab bar — consumed by reporter.py (static HTML)
# and injected into Jinja globals in server.py (live Flask routes).
NAV_TABS = [
    ("All Transactions", "/transactions",               "transactions",    None),
    ("Review",           "/review",                    "review",          None),
    ("Transfers",        "/transfers",                 "transfers",       "transfers"),
    ("FY Summary",       "/fy-summary",                "fy_summary",      "business"),
    ("Net Worth",        "/net-worth",                 "net_worth",       None),
    ("Merchants",        "/merchants",                 "merchants",       None),
    ("Tags",             "/tags",                      "tags",            None),
    ("Year on Year",     "/year-comparison",           "year_comparison", None),
    ("Reconciliation",  "/reconciliation",            "reconciliation",  None),
    ("Commitments",      "/commitments",               "commitments",     "commitments"),
    ("Subscriptions",    "/subscriptions",             "subscriptions",   "commitments"),
    ("Reimbursements",   "/reimbursements",            "reimbursements",  "business"),
    ("Superseded",       "/superseded-pairs",          "superseded_pairs","transfers"),
    ("Recommendations",  "/recommendations",           "recommendations", "recommendations"),
    ("Coverage",         "/coverage",                  "coverage",        "coverage"),
    ("Capital Gains",    "/capital-gains",             "capital_gains",   "investments"),
    ("Franking Credits", "/franking-credits",          "franking_credits","investments"),
    ("Portfolio",        "/portfolio",                 "portfolio",       "investments"),
    ("Cash Flow",        "/cash-flow",                 "cash_flow",       None),
    ("Goals & Loans",    "/financial-goals",           "goals",           "goals"),
    ("Debt Payoff",      "/debt-payoff",               "debt_payoff",     "goals"),
]

SETTINGS_TABS = [
    ("Accounts",       "/settings/accounts",        "accounts",       None),
    ("Data Sources",   "/data-sources",             "data_sources",   None),
    ("Merchant Rules", "/settings/merchant-rules",  "merchant_rules", None),
    ("Budgets",        "/settings/budgets",         "budgets",        "budgets"),
    ("Modules",        "/settings/modules",         "modules",        None),
    ("Bank Profiles",  "/settings/bank-profiles",   "bank_profiles",  None),
    ("Encryption",     "/settings/encryption",      "encryption",     None),
]


SUBCATS: dict[str, list[str]] = {
    "Bank Fees & Charges":    ["Monthly Fee", "ATM Fee", "International Transaction", "Overdraft Fee", "Other"],
    "Bank Interest Charged":  ["Credit Card Interest", "Loan Interest", "Other"],
    "Board & Lodging":        ["Director's Fees", "Board Meeting Fee", "Other"],
    "Business Expense":       ["Office Supplies", "Software & Subscriptions", "Equipment", "Travel", "Meals & Entertainment", "Professional Services", "Marketing", "Other"],
    "Business Reimbursement": ["Employer Reimbursement", "Expense Claim", "Other"],
    "Dining Out":             ["Breakfast", "Lunch", "Dinner", "Coffee & Drinks", "Takeaway", "Other"],
    "Donations":              ["DGR Charity", "Fundraising", "Community Giving", "Other"],
    "Education":              ["Tuition", "Books & Materials", "Online Courses", "School Fees", "Other"],
    "Entertainment":          ["Cinema", "Events & Concerts", "Sports", "Hobbies", "Gaming", "Other"],
    "Gifts Given":            ["Birthday Present", "Alcohol / Bottle Shop", "Florist", "Other"],
    "Gifts Received":         ["Birthday Money", "Cash Present", "Other"],
    "Groceries":              ["Supermarket", "Fresh Produce", "Specialty Food", "Alcohol", "Other"],
    "Health":                 ["GP / Doctor", "Dentist", "Optometrist", "Pharmacy", "Hospital", "Gym & Fitness", "Mental Health", "Other"],
    "Housing":                ["Rent", "Mortgage", "Council Rates", "Strata / Body Corporate", "Maintenance & Repairs", "Cleaning", "Other"],
    "Income":                 ["Trust Distribution", "Dividend", "Rental Income", "Freelance", "Other"],
    "Insurance":              ["Home & Contents", "Car", "Health", "Life", "Income Protection", "Travel", "Pet", "Other"],
    "Interest Income":        ["Savings Interest", "Term Deposit", "Bond Interest", "Other"],
    "Family Loan Received":   ["Other"],
    "Family Loan Repayment":  ["Other"],
    "Investment":             ["Shares", "ETF", "Managed Fund", "Crypto", "Property", "Other"],
    "Miscellaneous":          ["Other"],
    "Personal Care":          ["Haircut & Grooming", "Beauty & Cosmetics", "Clothing", "Shoes & Accessories", "Other"],
    "Subscriptions":          ["Streaming", "Software", "Membership", "News & Media", "Gaming", "Other"],
    "Transfers":              ["Internal Transfer", "Savings", "Loan Repayment", "Other"],
    "Transport":              ["Fuel", "Public Transport", "Parking", "Taxi / Rideshare", "Car Maintenance", "Registration & CTP", "Other"],
    "Travel":                 ["Flights", "Accommodation", "Car Hire", "Activities & Tours", "Travel Insurance", "Other"],
    "Utilities":              ["Electricity", "Gas", "Water", "Internet", "Mobile / Phone", "Other"],
}

# Categories never counted as discretionary spending (income, pass-through, non-expense flows).
EXCLUDE_FROM_SPEND: frozenset[str] = frozenset({
    "Income", "Board & Lodging", "Interest Income", "Business Reimbursement",
    "Family Loan Received", "Transfers", "Investment",
})

# All valid category names — single source of truth derived from SUBCATS keys.
VALID_CATEGORIES: frozenset[str] = frozenset(SUBCATS.keys())

# Sub-categories assigned by the system (not the AI) — always preserved even
# when they don't appear in the SUBCATS list for their parent category.
SYSTEM_SUBCATS: frozenset[str] = frozenset({"Reversal", "Refund", "Pay-in-4"})

# Flat lookup: (category, sub_category) → True for fast membership testing
_VALID_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (cat, sub) for cat, subs in SUBCATS.items() for sub in subs
)


def is_valid_subcat(category: str, sub_category: str) -> bool:
    """Return True if sub_category is valid for category.

    Empty string, system-assigned values (Reversal/Refund/Pay-in-4), and any
    sub_category that appears in SUBCATS[category] are all valid.
    """
    if not sub_category:
        return True
    if sub_category in SYSTEM_SUBCATS:
        return True
    return (category, sub_category) in _VALID_PAIRS


def md_to_html(md: str) -> str:
    """Convert the markdown subset used in recommendations to HTML."""
    lines = md.splitlines()
    out = []
    in_ul = in_ol = in_table = False

    def close_lists():
        nonlocal in_ul, in_ol, in_table
        if in_ul:  out.append("</ul>");  in_ul = False
        if in_ol:  out.append("</ol>");  in_ol = False
        if in_table: out.append("</tbody></table>"); in_table = False

    def inline(text):
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*([^*]+)\*",     r"<em>\1</em>",         text)
        text = re.sub(r"`([^`]+)`",       r"<code>\1</code>",      text)
        return text

    for line in lines:
        if line.startswith("### "):
            close_lists(); out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            close_lists(); out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            close_lists(); out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"[-:]+$", c) for c in cells if c):
                if not in_table and out and out[-1].startswith("<tr>"):
                    header_row = out.pop()
                    out.append(f"<thead>{header_row}</tr></thead><tbody>")
                    in_table = True
                continue
            row_html = "".join(f"<td>{inline(c)}</td>" for c in cells)
            if not in_table:
                close_lists()
                out.append("<table><thead>")
                out.append(f"<tr>{row_html}")
                in_table = True
            else:
                out.append(f"<tr>{row_html}</tr>")
        elif re.match(r"^[-*] ", line):
            if not in_ul: close_lists(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(line[2:])}</li>")
        elif re.match(r"^\d+\. ", line):
            if not in_ol: close_lists(); out.append("<ol>"); in_ol = True
            out.append(f"<li>{inline(re.sub(r'^\d+\.\s+', '', line))}</li>")
        elif re.match(r"^---+$", line.strip()):
            close_lists(); out.append("<hr>")
        elif not line.strip():
            close_lists(); out.append("")
        else:
            close_lists(); out.append(f"<p>{inline(line)}</p>")

    close_lists()
    return "\n".join(out)
