"""Measure how well the open, re-usable place datasets cover the campaign area.

Overture Maps (places theme, CDLA-Permissive-2.0 / Apache-2.0 sources) and Foursquare
OS Places (Apache-2.0) are published as Parquet files on public S3 buckets. This script
reads only the rows inside the campaign's bounding box and prints counts; it never
prints contact values (the repository logs are public).

Usage: python scripts/ci/opendata_probe.py [config.toml]
"""
from __future__ import annotations

import math
import re
import sys
import time
import xml.etree.ElementTree as ET

import duckdb
import requests

sys.path.insert(0, ".")
from leadgen.config import load_config  # noqa: E402

OVERTURE = "overturemaps-us-west-2"
FSQ = "fsq-os-places-us-east-1"


def releases(bucket: str, prefix: str = "release/") -> list[str]:
    r = requests.get(f"https://{bucket}.s3.amazonaws.com/", params={"list-type": "2", "prefix": prefix, "delimiter": "/"}, timeout=60)
    r.raise_for_status()
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    root = ET.fromstring(r.content)
    return sorted(p.text[len(prefix):].strip("/") for p in root.findall(".//s3:CommonPrefixes/s3:Prefix", ns))


def bbox(cfg):
    lat, lng = cfg["area"]["center"]
    r = float(cfg["area"]["radius_km"])
    dlat = r / 111.0
    dlng = r / (111.32 * math.cos(math.radians(lat)))
    return lng - dlng, lat - dlat, lng + dlng, lat + dlat


def con():
    c = duckdb.connect()
    c.execute("INSTALL httpfs; LOAD httpfs;")
    return c


def show(c, sql, title):
    t0 = time.time()
    print(f"\n--- {title}")
    try:
        rows = c.execute(sql).fetchall()
        cols = [d[0] for d in c.description]
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join("" if v is None else str(v) for v in r))
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}"[:800])
        rows = []
    print(f"({time.time() - t0:.1f}s)")
    return rows


def overture(cfg):
    rels = [r for r in releases(OVERTURE) if re.match(r"\d{4}-\d{2}-\d{2}", r)]
    print("Overture releases (latest 3):", rels[-3:])
    rel = rels[-1]
    xmin, ymin, xmax, ymax = bbox(cfg)
    c = con()
    c.execute("SET s3_region='us-west-2';")
    src = f"read_parquet('s3://{OVERTURE}/release/{rel}/theme=places/type=place/*', hive_partitioning=1)"
    where = f"bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax}"
    show(c, f"DESCRIBE SELECT * FROM {src} LIMIT 1", f"Overture {rel} schema")
    c.execute(f"CREATE TABLE ov AS SELECT * FROM {src} WHERE {where}")
    show(c, """SELECT count(*) places,
                 count(*) FILTER (WHERE len(phones) > 0) with_phone,
                 count(*) FILTER (WHERE len(websites) > 0) with_website,
                 count(*) FILTER (WHERE len(socials) > 0) with_social,
                 count(*) FILTER (WHERE len(emails) > 0) with_email,
                 round(avg(confidence), 2) avg_confidence
               FROM ov""", "Overture: places in the campaign box")
    cols = {r[0] for r in c.execute("DESCRIBE ov").fetchall()}
    cat = "categories.primary" if "categories" in cols else ("basic_category" if "basic_category" in cols else "NULL")
    show(c, f"SELECT {cat} AS category, count(*) n, count(*) FILTER (WHERE len(phones) > 0) phone, "
            f"count(*) FILTER (WHERE len(websites) > 0) web, count(*) FILTER (WHERE len(socials) > 0) social "
            f"FROM ov GROUP BY 1 ORDER BY 2 DESC LIMIT 40", "Overture: top categories")
    words = "cafe|coffee|restaurant|banquet|wedding|event|interior|architect|hotel|resort|cowork|decor"
    show(c, f"SELECT {cat} AS category, count(*) n, count(*) FILTER (WHERE len(phones) > 0) phone, "
            f"count(*) FILTER (WHERE len(websites) > 0) web, count(*) FILTER (WHERE len(socials) > 0) social "
            f"FROM ov WHERE regexp_matches(lower(coalesce({cat}, '')), '{words}') GROUP BY 1 ORDER BY 2 DESC LIMIT 40",
         "Overture: categories relevant to the campaign")
    if "operating_status" in cols:
        show(c, "SELECT operating_status, count(*) FROM ov GROUP BY 1 ORDER BY 2 DESC", "Overture: operating status")
    show(c, "SELECT (SELECT string_agg(DISTINCT s.dataset, ',') FROM unnest(sources) t(s)) src, count(*) FROM ov GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
         "Overture: sources")
    show(c, f"SELECT names.primary AS name, {cat} AS category, len(phones) > 0 AS phone, len(websites) > 0 AS web, "
            f"len(socials) > 0 AS social, round(confidence, 2) conf FROM ov WHERE regexp_matches(lower(coalesce({cat}, '')), 'cafe|restaurant|banquet') "
            f"AND bbox.ymin BETWEEN 22.54 AND 22.58 AND bbox.xmin BETWEEN 88.34 AND 88.37 LIMIT 25",
         "Overture: sample around Esplanade / Park Street (names only)")


def fsq(cfg):
    rels = [r for r in releases(FSQ) if r.startswith("dt=")]
    print("\nFSQ OS Places releases (latest 3):", rels[-3:])
    if not rels:
        print("no public FSQ releases listed")
        return
    rel = rels[-1]
    xmin, ymin, xmax, ymax = bbox(cfg)
    c = con()
    c.execute("SET s3_region='us-east-1';")
    src = f"read_parquet('s3://{FSQ}/release/{rel}/places/parquet/*.parquet')"
    show(c, f"DESCRIBE SELECT * FROM {src} LIMIT 1", f"FSQ {rel} schema")
    c.execute(f"CREATE TABLE fs AS SELECT * FROM {src} WHERE latitude BETWEEN {ymin} AND {ymax} AND longitude BETWEEN {xmin} AND {xmax}")
    cols = {r[0] for r in c.execute("DESCRIBE fs").fetchall()}
    closed = "date_closed IS NULL" if "date_closed" in cols else "true"
    show(c, f"""SELECT count(*) places, count(*) FILTER (WHERE {closed}) open_places,
                  count(tel) with_tel, count(website) with_website, count(email) with_email,
                  count(instagram) with_instagram, count(facebook_id) with_facebook
                FROM fs""", "FSQ: places in the campaign box")
    if "fsq_category_labels" in cols:
        show(c, f"SELECT lbl, count(*) n, count(tel) tel, count(website) web, count(instagram) ig FROM "
                f"(SELECT unnest(fsq_category_labels) lbl, tel, website, instagram FROM fs WHERE {closed}) "
                f"WHERE regexp_matches(lower(lbl), 'caf|coffee|restaurant|banquet|wedding|event|interior|architect|hotel|resort|cowork|decor') "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 40", "FSQ: relevant categories")
    if "date_refreshed" in cols:
        show(c, f"SELECT substr(CAST(date_refreshed AS VARCHAR), 1, 4) yr, count(*) FROM fs WHERE {closed} GROUP BY 1 ORDER BY 1 DESC LIMIT 10",
             "FSQ: last refreshed (year)")


def overture_taxonomy(cfg):
    """Fine-grained category codes (taxonomy.primary) relevant to the campaign, with samples."""
    rel = [r for r in releases(OVERTURE) if re.match(r"\d{4}-\d{2}-\d{2}", r)][-1]
    xmin, ymin, xmax, ymax = bbox(cfg)
    c = con()
    c.execute("SET s3_region='us-west-2';")
    src = f"read_parquet('s3://{OVERTURE}/release/{rel}/theme=places/type=place/*', hive_partitioning=1)"
    c.execute(f"CREATE TABLE ov AS SELECT names.primary AS name, taxonomy, basic_category, confidence, phones, websites, emails, "
              f"socials, brand.names.primary AS brand, operating_status FROM {src} "
              f"WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax}")
    words = ("caf|coffee|tea_|restaurant|eatery|dining|bistro|lounge|bar$|pub|banquet|wedding|event|party|venue|"
             "conven|interior|design|architect|decor|hotel|resort|lodg|guest|hostel|cowork|office_space|shared_office|"
             "caterer|florist|landscap|garden|nursery")
    show(c, f"SELECT taxonomy.primary AS code, basic_category, count(*) n, count(*) FILTER (WHERE len(phones) > 0) phone, "
            f"count(*) FILTER (WHERE len(emails) > 0) email, count(*) FILTER (WHERE len(websites) > 0) web, "
            f"round(avg(confidence), 2) conf FROM ov WHERE regexp_matches(lower(coalesce(taxonomy.primary, '')), '{words}') "
            f"GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 80", "Overture taxonomy codes relevant to the campaign")
    show(c, "SELECT CASE WHEN confidence < 0.2 THEN '0.0-0.2' WHEN confidence < 0.4 THEN '0.2-0.4' WHEN confidence < 0.6 THEN '0.4-0.6' "
            "WHEN confidence < 0.8 THEN '0.6-0.8' ELSE '0.8-1.0' END AS conf, count(*) n, count(*) FILTER (WHERE len(phones) > 0) phone "
            "FROM ov GROUP BY 1 ORDER BY 1", "Overture confidence distribution (all places)")
    show(c, "SELECT brand, count(*) n FROM ov WHERE brand IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 25", "Overture brands (chains)")
    for code in ("interior_design", "banquet_hall", "wedding_planning", "event_planning", "coworking_space", "caterer", "cafe"):
        show(c, f"SELECT name, taxonomy.primary AS code, len(phones) > 0 phone, len(emails) > 0 email, len(websites) > 0 web, "
                f"round(confidence, 2) conf FROM ov WHERE taxonomy.primary ILIKE '%{code}%' ORDER BY confidence DESC LIMIT 12",
             f"sample names for codes like {code}")
    show(c, "SELECT taxonomy.primary AS code, count(*) n FROM ov WHERE regexp_matches(lower(name), "
            "'interior|banquet|cowork|decorat|event management|wedding planner|architect') GROUP BY 1 ORDER BY 2 DESC LIMIT 30",
         "codes used for places whose NAME says interior/banquet/coworking/decorator/event/architect")


def overture_lookup(cfg):
    """Codes of specific places (quality checks of the category mapping)."""
    rel = [r for r in releases(OVERTURE) if re.match(r"\d{4}-\d{2}-\d{2}", r)][-1]
    xmin, ymin, xmax, ymax = bbox(cfg)
    c = con()
    c.execute("SET s3_region='us-west-2';")
    names = ["Assam Petro Chemicals", "International Telecom Network", "Arihant Building", "Dhall Agencies", "Sri Gopal Travels",
             "ABM Sales Corporation", "Roshni Advertising", "AkantoApon", "B You", "RedMagma Productions", "Universal Fountain",
             "Afreen Restaurant and Banquet", "The new estern caterer", "Jamuna Banquets", "The Legacy Banquet"]
    show(c, f"SELECT names.primary AS name, taxonomy.primary AS code, basic_category, taxonomy.alternates AS alt, round(confidence, 2) conf "
            f"FROM read_parquet('s3://{OVERTURE}/release/{rel}/theme=places/type=place/*', hive_partitioning=1) "
            f"WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax} "
            f"AND names.primary IN ({', '.join(repr(n) for n in names)})", "codes of reviewed places")
    show(c, f"SELECT taxonomy.primary AS code, count(*) n FROM read_parquet('s3://{OVERTURE}/release/{rel}/theme=places/type=place/*', "
            f"hive_partitioning=1) WHERE bbox.xmin BETWEEN {xmin} AND {xmax} AND bbox.ymin BETWEEN {ymin} AND {ymax} "
            f"AND basic_category = 'event_or_party_service' GROUP BY 1 ORDER BY 2 DESC LIMIT 30", "codes under basic event_or_party_service")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "lookup":
        overture_lookup(load_config(sys.argv[1]))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[2] == "taxonomy":
        overture_taxonomy(load_config(sys.argv[1]))
        sys.exit(0)
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config/plant-parlour.toml")
    print("campaign box (lng/lat):", [round(v, 3) for v in bbox(cfg)])
    for fn in (overture, fsq):
        try:
            fn(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"{fn.__name__} failed: {type(exc).__name__}: {exc}"[:800])
