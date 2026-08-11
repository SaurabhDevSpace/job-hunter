"""Work-authorization gate: keep only postings a candidate can legally hold.

The candidate for this deployment can work in India only, so a "remote" posting
that is really "Remote - US" is noise, and an onsite/hybrid role in Berlin is
unreachable no matter how well the skills match.

Sources already carry the answer, but in wildly different shapes — the aggregator
boards put the *candidate requirement* in the location field ("USA Only",
"Worldwide", "Remote - UK"), JobSpy emits ISO-ish codes ("KA, IN", "US, TX,
Austin"), and Workday emits "India - Hyderabad" or "United Kingdom - London".
Everything below normalizes those into a set of countries a posting will hire
from, then compares it against the candidate's home country.

Verdicts are deliberately three-valued. A bare "Remote" with no stated
restriction is *unclear*, not ineligible: dropping those would discard most
company-ATS postings, which rarely state geography in the location field. Only a
positively-identified foreign restriction removes a job.
"""
from __future__ import annotations

import re

# --- countries -------------------------------------------------------------
# Only the terms that actually distinguish a country are listed; this is a
# matcher, not an atlas.
COUNTRY_TERMS: dict[str, tuple[str, ...]] = {
    "in": ("india", "indian", "ind", "bharat"),
    "us": ("usa", "u.s.a", "u.s", "united states", "america", "american", "us"),
    "ca": ("canada", "canadian"),
    "mx": ("mexico", "mexican"),
    "br": ("brazil", "brasil", "brazilian"),
    "ar": ("argentina",),
    "cl": ("chile",),
    "co": ("colombia",),
    "pe": ("peru",),
    "uy": ("uruguay",),
    "cr": ("costa rica",),
    "ni": ("nicaragua",),
    "gt": ("guatemala",),
    "uk": ("united kingdom", "uk", "britain", "british", "england", "scotland",
           "wales", "northern ireland", "gb"),
    "ie": ("ireland", "irish"),
    "de": ("germany", "german", "deutschland"),
    "fr": ("france", "french"),
    "es": ("spain", "spanish", "espana", "españa"),
    "pt": ("portugal", "portuguese"),
    "it": ("italy", "italian", "italia"),
    "nl": ("netherlands", "holland", "dutch"),
    "be": ("belgium", "belgian"),
    "ch": ("switzerland", "swiss"),
    "at": ("austria", "austrian"),
    "se": ("sweden", "swedish"),
    "no": ("norway", "norwegian"),
    "dk": ("denmark", "danish"),
    "fi": ("finland", "finnish"),
    "pl": ("poland", "polish", "polska"),
    "cz": ("czech", "czechia"),
    "sk": ("slovakia",),
    "hu": ("hungary", "hungarian"),
    "ro": ("romania", "romanian"),
    "bg": ("bulgaria",),
    "gr": ("greece",),
    "hr": ("croatia",),
    "rs": ("serbia",),
    "ua": ("ukraine", "ukrainian"),
    "lt": ("lithuania",),
    "lv": ("latvia",),
    "ee": ("estonia",),
    "tr": ("turkey", "turkiye"),
    "il": ("israel", "israeli"),
    "ae": ("uae", "united arab emirates", "emirates"),
    "sa": ("saudi arabia", "saudi"),
    "qa": ("qatar",),
    "eg": ("egypt",),
    "za": ("south africa",),
    "ng": ("nigeria",),
    "ke": ("kenya",),
    "mu": ("mauritius",),
    "au": ("australia", "australian"),
    "nz": ("new zealand",),
    "jp": ("japan", "japanese", "jpn"),
    "kr": ("south korea", "korea", "korean"),
    "cn": ("china", "chinese", "prc"),
    "hk": ("hong kong",),
    "tw": ("taiwan",),
    "sg": ("singapore",),
    "my": ("malaysia",),
    "id": ("indonesia",),
    "th": ("thailand",),
    "vn": ("vietnam", "viet nam"),
    "ph": ("philippines", "filipino"),
    "pk": ("pakistan",),
    "bd": ("bangladesh",),
    "lk": ("sri lanka",),
    "np": ("nepal",),
}

# Indian states/UTs and their JobSpy 2-letter codes, so "KA, IN" and
# "Maharashtra, India" both resolve without needing a city list.
INDIA_SUBDIVISIONS: tuple[str, ...] = (
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "chhatisgarh", "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand",
    "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur",
    "meghalaya", "mizoram", "nagaland", "odisha", "orissa", "punjab",
    "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "uttaranchal", "west bengal",
    "andaman", "nicobar", "chandigarh", "dadra", "nagar haveli", "daman",
    "diu", "lakshadweep", "puducherry", "pondicherry", "jammu", "kashmir",
    "ladakh", "delhi ncr", "ncr",
)
INDIA_STATE_CODES: frozenset[str] = frozenset({
    "ap", "ar", "as", "br", "cg", "ga", "gj", "hr", "hp", "jh", "ka", "kl",
    "mp", "mh", "mn", "ml", "mz", "nl", "od", "or", "pb", "rj", "sk", "tn",
    "tr", "ts", "tg", "up", "uk", "ua", "wb", "an", "ch", "dh", "dd", "dl",
    "ld", "py", "jk", "la",
})

# Indian metros/tier-2 cities that appear bare, without a country suffix.
INDIA_CITIES: tuple[str, ...] = (
    "bengaluru", "bangalore", "mumbai", "navi mumbai", "thane", "new delhi",
    "delhi", "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad",
    "hyderabad", "secunderabad", "chennai", "kolkata", "pune", "pimpri",
    "ahmedabad", "surat", "vadodara", "rajkot", "jaipur", "lucknow", "kanpur",
    "nagpur", "indore", "bhopal", "patna", "ranchi", "raipur", "bhubaneswar",
    "guwahati", "dehradun", "chandigarh", "ludhiana", "amritsar", "jalandhar",
    "mohali", "panchkula", "kochi", "cochin", "ernakulam", "kozhikode",
    "calicut", "thiruvananthapuram", "trivandrum", "thrissur", "kollam",
    "coimbatore", "madurai", "tiruchirappalli", "trichy", "salem", "tirupur",
    "vellore", "visakhapatnam", "vizag", "vijayawada", "guntur", "tirupati",
    "mysore", "mysuru", "mangalore", "mangaluru", "hubli", "belgaum",
    "udaipur", "jodhpur", "kota", "ajmer", "bikaner", "gwalior", "jabalpur",
    "ujjain", "aurangabad", "nashik", "nasik", "solapur", "kolhapur",
    "sangli", "satara", "ulhasnagar", "kalyan", "vasai", "virar",
    "meerut", "agra", "varanasi", "prayagraj", "allahabad", "bareilly",
    "aligarh", "moradabad", "gorakhpur", "jhansi", "roorkee", "haridwar",
    "rishikesh", "shimla", "jammu", "srinagar", "siliguri", "durgapur",
    "asansol", "howrah", "jamshedpur", "dhanbad", "bokaro", "rourkela",
    "cuttack", "vapi", "vapi gidc", "ankleshwar", "bharuch", "jamnagar",
    "bhavnagar", "gandhinagar", "savli", "halol", "silvassa", "daman",
    "hosur", "sriperumbudur", "oragadam", "chakan", "chinchwad", "waluj",
    "korba", "bhilai", "bilaspur", "sambalpur", "bhadreswar", "bhiwadi",
    "patiala", "panipat", "sonipat", "rewari", "manesar", "neemrana",
    "puducherry", "pondicherry", "goa", "panaji", "vasco", "margao",
    "bardez", "candolim", "tiruvettipuram", "puliyur", "walajapet",
)

# --- core helpers ----------------------------------------------------------

_WORD = re.compile(r"[a-z']+")
_LOWER = re.compile(r"[a-z0-9 ]+")

def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 2}

def _codes_in(text: str) -> set[str]:
    """Extract 2-letter ISO-like codes like 'KA, IN' -> {'ka','in'}."""
    return {c.lower() for c in re.findall(r"\b([a-z]{2})\b", text.upper()) if len(c) == 2}

# --- public API ------------------------------------------------------------

Eligibility = tuple[str, list[str]]  # ('eligible'|'ineligible'|'unclear', countries_hiring_from)

def eligible(location: str, description: str = "", home_country: str = "in") -> Eligibility:
    """
    Return (verdict, countries_found).

    - 'eligible'    : posting is unrestricted OR restricts to `home_country`.
    - 'ineligible'  : posting positively restricts hiring to a country != home.
    - 'unclear'     : no country terms found (most company ATS postings). We do
      NOT drop these — they are the majority of hidden-market jobs and are
      frequently open to any remote worker unless the JD text says otherwise.
    """
    blob = f"{location} {description}".lower()
    tokens = _tokenize(blob)
    codes = _codes_in(blob)
    found = set()

    # 1. Country names / adjectives
    for country, terms in COUNTRY_TERMS.items():
        if any(t in tokens for t in terms):
            found.add(country)

    # 2. ISO codes (strong signal, esp. JobSpy)
    found |= {c for c in codes if c in COUNTRY_TERMS}

    # 3. India subdivisions / state codes
    if any(t in tokens for t in INDIA_SUBDIVISIONS):
        found.add("in")
    if any(c in codes for c in INDIA_STATE_CODES):
        found.add("in")

    # 4. Indian cities (bare city + "Remote" e.g. "Bengaluru" alone)
    if any(city in tokens for city in INDIA_CITIES):
        found.add("in")

    # 5. Explicitly unrestricted *location* wording (Worldwide / Anywhere).
    # Scope this to the location field only: generic JD prose like "global team"
    # must not make an otherwise country-restricted role appear unrestricted.
    location_lc = (location or "").lower()
    if any(ut in location_lc for ut in UNRESTRICTED_TERMS):
        # Check for restricting phrases in the FULL text (location + desc)
        restricting_phrases = (
            "us only", "usa only", "u.s. only", "uk only", "eu only",
            "emea only", "apac only", "latam only", "gcc only", "anz only",
            "canada only", "australia only", "germany only", "france only",
            "singapore only", "must be located", "must be based", "must reside",
            "authorized to work", "work authorization", "citizens only",
            "visa sponsorship", "visa required", "timezone", "utc-")
        restricting = any(p in blob for p in restricting_phrases)
        if not restricting:
            return "eligible", ["in"]

    # 5. Macro-region aliases
    for alias, region_codes in (
        ("americas", ["us", "ca", "mx"]),
        ("emea", ["de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no",
                  "dk", "fi", "pl", "cz", "ie", "uk"]),
        ("apac", ["sg", "my", "id", "th", "vn", "ph", "jp", "kr", "cn", "hk",
                  "tw", "au", "nz"]),
        ("latam", ["br", "mx", "ar", "cl", "co", "pe", "uy"]),
        ("gcc", ["ae", "sa", "qa", "kw", "bh", "om"]),
        ("anz", ["au", "nz"]),
        ("usa", ["us"]),
        ("us", ["us"]),
        ("uk", ["uk"]),
        ("canada", ["ca"]),
        ("eu", ["de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no",
                "dk", "fi", "pl", "cz", "ie"]),
    ):
        if alias in tokens or alias.replace(" ", "") in tokens:
            found |= {c for c in region_codes}

    # --- verdict ---
    if not found:
        return "unclear", []
    if found == {"in"}:
        return "eligible", ["in"]
    if "in" in found and len(found) == 1:
        return "eligible", ["in"]
    if found and "in" not in found:
        return "ineligible", sorted(found)
    # India plus others (e.g. "Worldwide" / "Anywhere in the World") -> eligible
    return "eligible", sorted(found)

# "Worldwide" / "Anywhere" are unrestricted unless combined with a
# restricting clause ("Worldwide, but must be in US timezones" etc.). That
# complexity is better handled by the text scan; here we just suppress the
# macro-region expansion when these unrestricted keywords appear alone.
UNRESTRICTED_TERMS = {"worldwide", "anywhere", "anywhere in the world",
                      "anywhere worldwide", "work from anywhere"}
