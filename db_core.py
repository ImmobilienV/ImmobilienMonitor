
import os
import hashlib
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Float, Integer,
    BigInteger, Text, select, text, and_
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///immobilien_v3.db")

# Railway/Heroku-style postgres URLs sometimes still use postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

metadata = MetaData()

listings = Table(
    "listings", metadata,
    Column("id", String(64), primary_key=True),
    Column("source_key", String(255), nullable=False),
    Column("portal", String(255)),
    Column("external_id", String(255)),
    Column("title", Text),
    Column("object_type", String(255)),
    Column("offer_type", String(50)),
    Column("city", String(255)),
    Column("postal_code", String(20)),
    Column("street", Text),
    Column("price_eur", Float),
    Column("living_area_m2", Float),
    Column("plot_area_m2", Float),
    Column("rooms", Float),
    Column("year_built", Integer),
    Column("price_per_m2", Float),
    Column("provider", Text),
    Column("url", Text),
    Column("image_url", Text),
    Column("description", Text),
    Column("first_seen", String(64)),
    Column("last_seen", String(64)),
    Column("status", String(50), default="aktiv"),
    Column("inactive_since", String(64)),
    Column("seen_count", Integer, default=1),
    Column("source_type", String(100)),
)

observations = Table(
    "observations", metadata,
    Column("observation_id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("listing_id", String(64), nullable=False),
    Column("observed_at", String(64), nullable=False),
    Column("price_eur", Float),
    Column("living_area_m2", Float),
    Column("plot_area_m2", Float),
    Column("rooms", Float),
    Column("status", String(50)),
    Column("source_run", String(64)),
)

scan_runs = Table(
    "scan_runs", metadata,
    Column("run_id", String(64), primary_key=True),
    Column("source_key", String(255), nullable=False),
    Column("started_at", String(64), nullable=False),
    Column("finished_at", String(64)),
    Column("mode", String(100)),
    Column("found_count", Integer, default=0),
    Column("imported_count", Integer, default=0),
    Column("error_count", Integer, default=0),
)

sources = Table(
    "sources", metadata,
    Column("source_id", String(64), primary_key=True),
    Column("source_key", String(255), nullable=False, unique=True),
    Column("source_type", String(50), nullable=False),
    Column("source_url", Text),
    Column("enabled", Integer, default=1),
    Column("complete_snapshot", Integer, default=1),
    Column("max_urls", Integer, default=100),
    Column("created_at", String(64)),
)

def init_db():
    metadata.create_all(engine)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def make_id(*parts):
    raw = "|".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def read_df(sql, params=None):
    with engine.connect() as con:
        return pd.read_sql(text(sql), con, params=params or {})

def listings_df():
    return read_df("SELECT * FROM listings ORDER BY last_seen DESC")

def history_df():
    return read_df("""
        SELECT o.*, l.source_key, l.portal, l.title, l.city, l.url
        FROM observations o
        JOIN listings l ON l.id = o.listing_id
        ORDER BY o.observed_at DESC
    """)

def runs_df():
    return read_df("SELECT * FROM scan_runs ORDER BY started_at DESC")

def sources_df():
    return read_df("SELECT * FROM sources ORDER BY source_key")

def _upsert_stmt(table, values, key_cols):
    dialect = engine.dialect.name
    update_cols = {k: v for k, v in values.items() if k not in key_cols}
    if dialect == "postgresql":
        stmt = pg_insert(table).values(**values)
        return stmt.on_conflict_do_update(index_elements=list(key_cols), set_=update_cols)
    stmt = sqlite_insert(table).values(**values)
    return stmt.on_conflict_do_update(index_elements=list(key_cols), set_=update_cols)

def upsert_listing(item, run_id=None):
    init_db()
    now = utcnow()
    with engine.begin() as con:
        old = con.execute(select(listings).where(listings.c.id == item["id"])).mappings().first()
        values = dict(item)
        values["first_seen"] = old["first_seen"] if old else now
        values["last_seen"] = now
        values["status"] = "aktiv"
        values["inactive_since"] = None
        values["seen_count"] = ((old["seen_count"] or 0) + 1) if old else 1

        allowed = {c.name for c in listings.columns}
        values = {k: v for k, v in values.items() if k in allowed}
        con.execute(_upsert_stmt(listings, values, {"id"}))
        con.execute(observations.insert().values(
            listing_id=item["id"],
            observed_at=now,
            price_eur=item.get("price_eur"),
            living_area_m2=item.get("living_area_m2"),
            plot_area_m2=item.get("plot_area_m2"),
            rooms=item.get("rooms"),
            status="aktiv",
            source_run=run_id,
        ))

def start_run(source_key, mode):
    init_db()
    rid = make_id(source_key, utcnow(), mode)
    with engine.begin() as con:
        con.execute(scan_runs.insert().values(
            run_id=rid, source_key=source_key,
            started_at=utcnow(), mode=mode,
            found_count=0, imported_count=0, error_count=0
        ))
    return rid

def finish_run(run_id, found, imported, errors):
    with engine.begin() as con:
        con.execute(
            scan_runs.update()
            .where(scan_runs.c.run_id == run_id)
            .values(
                finished_at=utcnow(),
                found_count=found,
                imported_count=imported,
                error_count=errors
            )
        )

def mark_missing_inactive(source_key, seen_ids):
    if not seen_ids:
        return
    now = utcnow()
    with engine.begin() as con:
        rows = con.execute(
            select(listings.c.id, listings.c.inactive_since)
            .where(and_(listings.c.source_key == source_key, listings.c.status == "aktiv"))
        ).all()
        missing = [r[0] for r in rows if r[0] not in set(seen_ids)]
        if missing:
            con.execute(
                listings.update()
                .where(listings.c.id.in_(missing))
                .values(status="nicht mehr online", inactive_since=now)
            )

def save_source(source_key, source_type, source_url, enabled=True, complete_snapshot=True, max_urls=100):
    init_db()
    sid = make_id(source_key)
    values = dict(
        source_id=sid,
        source_key=source_key,
        source_type=source_type,
        source_url=source_url,
        enabled=1 if enabled else 0,
        complete_snapshot=1 if complete_snapshot else 0,
        max_urls=int(max_urls),
        created_at=utcnow(),
    )
    with engine.begin() as con:
        old = con.execute(select(sources).where(sources.c.source_id == sid)).mappings().first()
        if old:
            values["created_at"] = old["created_at"]
        con.execute(_upsert_stmt(sources, values, {"source_id"}))

def delete_source(source_id):
    with engine.begin() as con:
        con.execute(sources.delete().where(sources.c.source_id == source_id))

def clear_all():
    with engine.begin() as con:
        con.execute(observations.delete())
        con.execute(listings.delete())
        con.execute(scan_runs.delete())
