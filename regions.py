"""
Regions
=======
Turns Wialon's free-typed region text into a city, for the portal dashboard and the Excel dashboard
alike, so the two never disagree.

A vehicle's region is the depot region someone assigned it in Wialon: where it works. It is not where
a violation happened, and not where the vehicle is registered. It is read from, in order:

  1. the vehicle's "Region" field (an admin field on most vehicles, a custom field on a few)
  2. any other field whose value names a city (a few vehicles have the region typed into the name box)
  3. a regional unit group the vehicle belongs to ("Karachi", "WPS Foods Factory Lahore", ...)
"""

from collections import Counter

# A region tag names a site ("WPS/Foods Factory Lahore") or a route ("Peshawar/Islamabad"); both are
# grouped onto the first city that appears in them.
CITIES = ["Rahim Yar Khan", "Karachi", "Lahore", "Peshawar", "Islamabad", "Jhelum", "Faisalabad",
          "Hyderabad", "Sahiwal", "Multan", "Gujranwala", "Abbottabad", "Sukkur", "Quetta",
          "Sialkot", "Bahawalpur", "Sargodha", "Mardan", "Rawalpindi", "Nowshera"]

# Other spellings, Urdu names (Wialon returns many addresses in Urdu) and suburbs. Checked after the
# English names above, so a plain "Lahore Bypass" still resolves on its own.
ALIASES = {
    "Jehlum": "Jhelum",
    "کراچی": "Karachi", "لاہور": "Lahore",
    "پشاور": "Peshawar", "اسلام آباد": "Islamabad",
    "رحیم یار خان": "Rahim Yar Khan",
    "ملتان": "Multan", "فیصل آباد": "Faisalabad",
    "حیدرآباد": "Hyderabad", "ساہیوال": "Sahiwal",
    "گوجرانوالہ": "Gujranwala", "جہلم": "Jhelum",
    "ایبٹ آباد": "Abbottabad", "سکھر": "Sukkur",
    "کوئٹہ": "Quetta", "سیالکوٹ": "Sialkot",
    "بہاولپور": "Bahawalpur", "راولپنڈی": "Rawalpindi",
    "سرگودھا": "Sargodha", "مردان": "Mardan",
    "نوشہرہ": "Nowshera",
    "Khwaja Town": "Peshawar", "Mandian": "Abbottabad",
}

UNASSIGNED = "Unassigned"


def city_in(text):
    """The city named anywhere in text, by English name first, then by alias."""
    lowered = str(text or "").lower()
    for city in CITIES:
        if city.lower() in lowered:
            return city
    for alias, city in ALIASES.items():
        if alias.lower() in lowered:
            return city
    return None


def group_region(raw):
    """'WPS/Foods Factory Lahore' -> 'Lahore'; 'Peshawar/Islamabad' -> 'Peshawar'."""
    if not raw:
        return None
    parts = [p.strip() for p in str(raw).split("/") if p.strip()]
    parts = [p for p in parts if p.upper() != "WPS" and not p.upper().startswith("UFS")] or parts
    for part in parts:                       # first segment that names a city wins
        city = city_in(part)
        if city:
            return city
    return parts[0] if parts else None


def vehicle_regions(units, groups):
    """{unit id: {"region": city, "raw": text as typed in Wialon, "source": where it came from}}.
    units need flags 0x8 | 0x80 (custom and admin fields); groups need their member lists."""
    city_groups = {}
    for g in groups:
        city = city_in(g.get("nm") or "")
        if city:
            for uid in g.get("u") or []:
                city_groups.setdefault(uid, []).append((city, g["nm"]))

    out = {}
    for u in units:
        fields = [f for section in ("aflds", "flds") for f in (u.get(section) or {}).values()]
        raw = next(((f.get("v") or "").strip() for f in fields
                    if (f.get("n") or "").strip().lower() == "region" and (f.get("v") or "").strip()), "")
        source = "Region field"
        if not raw:
            raw = next(((f.get("v") or "").strip() for f in fields if city_in(f.get("v") or "")), "")
            source = "Other field"
        city = group_region(raw) if raw else None
        if not city and city_groups.get(u["id"]):
            city, raw = Counter(city_groups[u["id"]]).most_common(1)[0][0]
            source = "Regional group"
        if city:
            out[u["id"]] = {"region": city_in(city) or city, "raw": raw, "source": source}
    return out
