
import gzip
import io
import json
import os
import re
import socket
import ipaddress
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import pandas as pd
import requests
from bs4 import BeautifulSoup

from db_core import make_id

USER_AGENT = os.getenv(
    "IMMOMONITOR_USER_AGENT",
    "ImmobilienMonitor/3.0 (+compliance-first; contact=local-user)"
)
TIMEOUT = 20

def clean_text(x):
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return " ".join(clean_text(v) for v in x if v is not None)
    return re.sub(r"\s+", " ", str(x)).strip()

def parse_num(x):
    if x is None or x == "":
        return None
    if isinstance(x, (int, float)) and not pd.isna(x):
        return float(x)
    s = str(x).replace("\xa0", " ")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def parse_year(x):
    n = parse_num(x)
    if n is None:
        return None
    y = int(n)
    return y if 1700 <= y <= 2200 else None

def domain_of(url):
    try:
        return urlparse(url).hostname.lower().removeprefix("www.")
    except Exception:
        return ""

def is_public_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False, "Nur http/https URLs sind erlaubt."
        infos = socket.getaddrinfo(p.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "Private/lokale Netzwerkadressen werden nicht abgerufen."
        return True, ""
    except Exception as e:
        return False, f"URL konnte nicht sicher geprüft werden: {e}"

def robots_allowed(url):
    ok, reason = is_public_url(url)
    if not ok:
        return False, reason
    p = urlparse(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        r = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if r.status_code >= 400:
            return False, f"robots.txt nicht verlässlich abrufbar ({r.status_code})."
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url), robots_url
    except Exception as e:
        return False, f"robots.txt-Prüfung fehlgeschlagen: {e}"

def safe_get(url, binary=False):
    allowed, info = robots_allowed(url)
    if not allowed:
        raise PermissionError(f"Abruf nicht freigegeben: {info}")
    r = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    r.raise_for_status()
    ok, reason = is_public_url(r.url)
    if not ok:
        raise PermissionError(reason)
    return (r.content if binary else r.text), r.url

def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)

def jsonld_candidate(soup):
    best = (0, {})
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in walk_json(data):
            if not isinstance(obj, dict):
                continue
            typ = clean_text(obj.get("@type")).lower()
            score = 0
            if any(k in typ for k in ("house","apartment","residence","realestate","offer","product","accommodation")):
                score += 2
            if "offers" in obj:
                score += 2
            if "address" in obj:
                score += 1
            if "floorSize" in obj or "numberOfRooms" in obj:
                score += 2
            if obj.get("name") or obj.get("headline"):
                score += 1
            if score > best[0]:
                best = (score, obj)
    return best

def meta(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""

def nested(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur

def extract_listing_from_html(html, final_url, source_key, source_type="Website"):
    soup = BeautifulSoup(html, "html.parser")
    score, obj = jsonld_candidate(soup)

    title = clean_text(obj.get("name") or obj.get("headline")) if obj else ""
    title = title or meta(soup, "og:title", "twitter:title")
    desc = clean_text(obj.get("description")) if obj else ""
    desc = desc or meta(soup, "og:description", "description")
    visible = soup.get_text(" ", strip=True)

    if re.search(r"\b(Kaufpreis|Kaltmiete|Wohnfläche|Grundstücksfläche|Zimmer|Baujahr)\b", visible, re.I):
        score += 2
    if score < 3:
        return None

    image = obj.get("image") if obj else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    elif isinstance(image, list):
        image = image[0] if image else ""
    image = clean_text(image) or meta(soup, "og:image")

    offers = obj.get("offers") if obj else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}
    price = parse_num(offers.get("price") or nested(offers, "priceSpecification", "price"))

    address = obj.get("address") if obj else {}
    if not isinstance(address, dict):
        address = {}
    city = clean_text(address.get("addressLocality"))
    postal = clean_text(address.get("postalCode"))
    street = clean_text(address.get("streetAddress"))

    fs = obj.get("floorSize") if obj else None
    if isinstance(fs, dict):
        area = parse_num(fs.get("value"))
    else:
        area = parse_num(fs)

    rooms = parse_num(obj.get("numberOfRooms")) if obj else None
    year_built = parse_year(obj.get("yearBuilt")) if obj else None

    if area is None:
        m = re.search(r"(?:Wohnfläche|Wohnflaeche)(?:\s*ca\.)?\s*:?\s*([\d.,]+)\s*m(?:²|2)", visible, re.I)
        if m: area = parse_num(m.group(1))
    plot = None
    m = re.search(r"(?:Grundstücksfläche|Grundstuecksflaeche)\s*:?\s*([\d.,]+)\s*m(?:²|2)", visible, re.I)
    if m: plot = parse_num(m.group(1))
    if rooms is None:
        m = re.search(r"([\d.,]+)\s*(?:Zimmer|Zi\.)", visible, re.I)
        if m: rooms = parse_num(m.group(1))
    if year_built is None:
        m = re.search(r"(?:Baujahr)\s*:?\s*(\d{4})", visible, re.I)
        if m: year_built = parse_year(m.group(1))
    if price is None:
        m = re.search(r"(?:Kaufpreis|Kaltmiete|Preis)\s*:?\s*([\d.\s]+(?:,\d+)?)\s*€", visible, re.I)
        if m: price = parse_num(m.group(1))

    provider = ""
    seller = obj.get("seller") if obj else None
    if isinstance(seller, dict):
        provider = clean_text(seller.get("name"))

    canonical = soup.find("link", attrs={"rel": "canonical"})
    canonical_url = canonical.get("href") if canonical and canonical.get("href") else final_url
    canonical_url = urljoin(final_url, canonical_url)

    typ = clean_text(obj.get("@type")) if obj else ""
    lower = f"{title} {desc}".lower()
    offer_type = "Miete" if ("miete" in lower or "rent" in lower) else (
        "Kauf" if ("kauf" in lower or "sale" in lower) else ""
    )

    external_id = ""
    for pat in [
        r"(?:expose|exposé|anzeige|listing|objekt)[/_-]?([a-z0-9-]{5,})",
        r"/(\d{6,})(?:[/?#]|$)",
    ]:
        m = re.search(pat, canonical_url, re.I)
        if m:
            external_id = m.group(1)
            break

    ppm = round(price / area, 2) if price and area and area > 0 else None
    return {
        "id": make_id(canonical_url or external_id or f"{title}|{city}|{price}|{area}"),
        "source_key": source_key,
        "portal": domain_of(canonical_url),
        "external_id": external_id,
        "title": title,
        "object_type": typ,
        "offer_type": offer_type,
        "city": city,
        "postal_code": postal,
        "street": street,
        "price_eur": price,
        "living_area_m2": area,
        "plot_area_m2": plot,
        "rooms": rooms,
        "year_built": year_built,
        "price_per_m2": ppm,
        "provider": provider,
        "url": canonical_url,
        "image_url": image,
        "description": desc[:5000],
        "source_type": source_type,
    }

def read_sitemap_urls(sitemap_url, max_urls=100):
    content, final_url = safe_get(sitemap_url, binary=True)
    if final_url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ET.fromstring(content)
    def tagname(tag):
        return tag.split("}")[-1].lower()
    locs = [clean_text(el.text) for el in root.iter() if tagname(el.tag) == "loc" and el.text]
    locs = [u for u in locs if domain_of(u) == domain_of(final_url)]

    if tagname(root.tag) == "sitemapindex":
        out = []
        for sub_url in locs[:20]:
            try:
                sub, sub_final = safe_get(sub_url, binary=True)
                if sub_final.endswith(".gz") or sub[:2] == b"\x1f\x8b":
                    sub = gzip.decompress(sub)
                subroot = ET.fromstring(sub)
                for el in subroot.iter():
                    if tagname(el.tag) == "loc" and el.text:
                        u = clean_text(el.text)
                        if domain_of(u) == domain_of(final_url):
                            out.append(u)
                            if len(out) >= max_urls:
                                return out
            except Exception:
                continue
        return out[:max_urls]
    return locs[:max_urls]

ALIASES = {
    "portal": ["portal","quelle","source"],
    "external_id": ["external_id","id","expose_id","anzeigen_id","objektnummer","object_id"],
    "title": ["title","titel","überschrift","ueberschrift"],
    "object_type": ["object_type","objektart","immobilienart","typ"],
    "offer_type": ["offer_type","angebotsart","kauf_miete","transaktion"],
    "city": ["city","ort","stadt","gemeinde"],
    "postal_code": ["postal_code","plz","postleitzahl"],
    "street": ["street","straße","strasse","adresse"],
    "price_eur": ["price_eur","preis","kaufpreis","miete","kaltmiete","price"],
    "living_area_m2": ["living_area_m2","wohnfläche","wohnflaeche","fläche","flaeche","wohnflaeche_m2"],
    "plot_area_m2": ["plot_area_m2","grundstücksfläche","grundstuecksflaeche","grundstück","grundstueck"],
    "rooms": ["rooms","zimmer","anzahl_zimmer"],
    "year_built": ["year_built","baujahr"],
    "provider": ["provider","anbieter","makler"],
    "url": ["url","link","expose_url"],
    "image_url": ["image_url","bild","bild_url"],
    "description": ["description","beschreibung","text"],
}

def normalize_dataframe(raw, source_key, portal_default=""):
    lower_map = {str(c).strip().lower(): c for c in raw.columns}
    data = {}
    for dest, aliases in ALIASES.items():
        src = next((lower_map[a.lower()] for a in aliases if a.lower() in lower_map), None)
        data[dest] = raw[src] if src is not None else pd.Series([""] * len(raw))
    df = pd.DataFrame(data)

    items = []
    for _, r in df.iterrows():
        url = clean_text(r["url"])
        portal = clean_text(r["portal"]) or portal_default or domain_of(url)
        price = parse_num(r["price_eur"])
        area = parse_num(r["living_area_m2"])
        external_id = clean_text(r["external_id"])
        title = clean_text(r["title"])
        city = clean_text(r["city"])
        key = url or external_id or f"{title}|{city}|{price}|{area}"
        items.append({
            "id": make_id(key),
            "source_key": source_key,
            "portal": portal,
            "external_id": external_id,
            "title": title,
            "object_type": clean_text(r["object_type"]),
            "offer_type": clean_text(r["offer_type"]),
            "city": city,
            "postal_code": clean_text(r["postal_code"]),
            "street": clean_text(r["street"]),
            "price_eur": price,
            "living_area_m2": area,
            "plot_area_m2": parse_num(r["plot_area_m2"]),
            "rooms": parse_num(r["rooms"]),
            "year_built": parse_year(r["year_built"]),
            "price_per_m2": round(price/area, 2) if price and area and area > 0 else None,
            "provider": clean_text(r["provider"]),
            "url": url,
            "image_url": clean_text(r["image_url"]),
            "description": clean_text(r["description"])[:5000],
            "source_type": "Dateiimport",
        })
    return items
