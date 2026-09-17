
import io
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from db_core import (
    init_db, listings_df, history_df, runs_df, sources_df,
    upsert_listing, start_run, finish_run, mark_missing_inactive,
    save_source, delete_source, clear_all
)
from scraping_core import (
    safe_get, extract_listing_from_html, read_sitemap_urls,
    normalize_dataframe
)

REGION_PRESET = [
    "Wangen im Allgäu", "Isny im Allgäu", "Leutkirch im Allgäu",
    "Kißlegg", "Bad Wurzach", "Argenbühl", "Waldburg", "Wolfegg",
    "Grünkraut", "Aitrach", "Aichstetten", "Amtzell", "Bodnegg",
    "Achberg", "Schlier"
]

def get_secret(name, default=""):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)

def login():
    configured = get_secret("APP_PASSWORD", "")
    if not configured:
        st.warning("Noch kein APP_PASSWORD gesetzt. Die App läuft derzeit ohne Login.")
        return True
    if st.session_state.get("auth_ok"):
        return True
    st.title("🏠 Immobilien-Monitor")
    pwd = st.text_input("Passwort", type="password")
    if st.button("Anmelden", type="primary"):
        if pwd == configured:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Passwort nicht korrekt.")
    return False

def add_price_change_columns(df, hist):
    if df.empty:
        return df
    df = df.copy()
    df["vorheriger_preis"] = pd.NA
    df["preisänderung_eur"] = pd.NA
    df["preisänderung_pct"] = pd.NA
    if hist.empty:
        return df
    h = hist.sort_values(["listing_id", "observed_at"])
    prev_map = {}
    for listing_id, grp in h.groupby("listing_id"):
        prices = [p for p in grp["price_eur"].tolist() if pd.notna(p)]
        if len(prices) >= 2:
            current = prices[-1]
            previous = None
            for p in reversed(prices[:-1]):
                if p != current:
                    previous = p
                    break
            if previous is not None:
                prev_map[listing_id] = previous
    for i, row in df.iterrows():
        prev = prev_map.get(row["id"])
        cur = row["price_eur"]
        if prev is not None and pd.notna(cur):
            df.at[i, "vorheriger_preis"] = prev
            delta = float(cur) - float(prev)
            df.at[i, "preisänderung_eur"] = delta
            df.at[i, "preisänderung_pct"] = round(delta / float(prev) * 100, 2) if prev else pd.NA
    return df

def days_online(first_seen, last_seen=None):
    try:
        a = pd.to_datetime(first_seen, utc=True)
        b = pd.Timestamp.now(tz="UTC") if not last_seen else pd.to_datetime(last_seen, utc=True)
        return max(0, (b - a).days)
    except Exception:
        return None

def excel_bytes(df, hist, runs):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Angebote")
        hist.to_excel(writer, index=False, sheet_name="Historie")
        runs.to_excel(writer, index=False, sheet_name="Abrufe")
        if not df.empty:
            active = df[df["status"] == "aktiv"].copy()
            summary = (
                active.groupby(["city", "object_type"], dropna=False)
                .agg(
                    Anzahl=("id", "count"),
                    Median_Preis_EUR=("price_eur", "median"),
                    Median_EUR_m2=("price_per_m2", "median"),
                    Median_Wohnflaeche_m2=("living_area_m2", "median"),
                    Median_Angebotstage=("angebotstage", "median"),
                ).reset_index()
            )
            summary.to_excel(writer, index=False, sheet_name="Marktauswertung")
    return bio.getvalue()

st.set_page_config(page_title="Immobilien-Monitor V3", page_icon="🏠", layout="wide")
init_db()
if not login():
    st.stop()

st.title("🏠 Immobilien-Monitor V3")
st.caption("Browserbasierte Angebotsmarkt-Beobachtung mit Cloud-Datenbank, Preisverlauf und regionaler Auswertung.")

with st.sidebar:
    page = st.radio(
        "Bereich",
        ["Markt-Dashboard", "Angebote", "Import", "Webquellen", "Preisverlauf", "Quellenverwaltung", "Einstellungen"]
    )
    st.divider()
    if get_secret("APP_PASSWORD", "") and st.button("Abmelden"):
        st.session_state["auth_ok"] = False
        st.rerun()

base = listings_df()
hist = history_df()
runs = runs_df()

if not base.empty:
    data = add_price_change_columns(base, hist)
    data["angebotstage"] = data.apply(
        lambda r: days_online(r["first_seen"], r["inactive_since"] if r["status"] != "aktiv" else None), axis=1
    )
else:
    data = base.copy()

if page == "Markt-Dashboard":
    st.subheader("Marktbeobachtung")
    if data.empty:
        st.info("Noch keine Angebote gespeichert. Unter „Import“ oder „Webquellen“ kannst du Daten einlesen.")
    else:
        active = data[data["status"] == "aktiv"].copy()
        c1, c2, c3 = st.columns(3)
        region_default = [x for x in REGION_PRESET if x in active["city"].dropna().unique().tolist()]
        selected_cities = c1.multiselect("Gemeinden", sorted(x for x in active["city"].dropna().unique() if x), default=region_default)
        selected_types = c2.multiselect("Objektarten", sorted(x for x in active["object_type"].dropna().unique() if x))
        selected_offer = c3.multiselect("Kauf / Miete", sorted(x for x in active["offer_type"].dropna().unique() if x))

        view = active.copy()
        if selected_cities:
            view = view[view["city"].isin(selected_cities)]
        if selected_types:
            view = view[view["object_type"].isin(selected_types)]
        if selected_offer:
            view = view[view["offer_type"].isin(selected_offer)]

        reduced = view[pd.to_numeric(view["preisänderung_eur"], errors="coerce") < 0]
        now = pd.Timestamp.now(tz="UTC")
        first_seen = pd.to_datetime(view["first_seen"], utc=True, errors="coerce")
        new30 = view[(now - first_seen).dt.days <= 30]

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Aktive Angebote", len(view))
        m2.metric("Neu ≤ 30 Tage", len(new30))
        m3.metric("Preissenkungen", len(reduced))
        med_ppm = view["price_per_m2"].median()
        m4.metric("Median €/m²", f"{med_ppm:,.0f}" if pd.notna(med_ppm) else "–")
        med_days = view["angebotstage"].median()
        m5.metric("Median Angebotsdauer", f"{med_days:.0f} Tage" if pd.notna(med_days) else "–")

        st.markdown("#### Gemeinden")
        summary = (
            view.groupby("city", dropna=False)
            .agg(
                Angebote=("id", "count"),
                Median_Preis=("price_eur", "median"),
                Median_EUR_m2=("price_per_m2", "median"),
                Median_Wohnfläche=("living_area_m2", "median"),
                Median_Angebotstage=("angebotstage", "median"),
            ).reset_index().sort_values("Angebote", ascending=False)
        )
        st.dataframe(summary, use_container_width=True, hide_index=True)

        chart = summary.dropna(subset=["Median_EUR_m2"]).set_index("city")["Median_EUR_m2"]
        if len(chart):
            st.bar_chart(chart)

        st.markdown("#### Objektarten")
        objsum = (
            view.groupby("object_type", dropna=False)
            .agg(
                Angebote=("id", "count"),
                Median_EUR_m2=("price_per_m2", "median"),
                Median_Preis=("price_eur", "median"),
            ).reset_index().sort_values("Angebote", ascending=False)
        )
        st.dataframe(objsum, use_container_width=True, hide_index=True)

elif page == "Angebote":
    st.subheader("Angebotsdatenbank")
    if data.empty:
        st.info("Noch keine Daten vorhanden.")
    else:
        f1, f2, f3, f4 = st.columns(4)
        cities = sorted(x for x in data["city"].dropna().unique() if x)
        sources = sorted(x for x in data["source_key"].dropna().unique() if x)
        statuses = sorted(x for x in data["status"].dropna().unique() if x)
        objtypes = sorted(x for x in data["object_type"].dropna().unique() if x)
        fc = f1.multiselect("Ort", cities)
        fs = f2.multiselect("Quelle", sources)
        fst = f3.multiselect("Status", statuses, default=["aktiv"] if "aktiv" in statuses else [])
        fo = f4.multiselect("Objektart", objtypes)

        view = data.copy()
        if fc: view = view[view["city"].isin(fc)]
        if fs: view = view[view["source_key"].isin(fs)]
        if fst: view = view[view["status"].isin(fst)]
        if fo: view = view[view["object_type"].isin(fo)]

        cols = [
            "status","city","postal_code","object_type","offer_type","title",
            "price_eur","vorheriger_preis","preisänderung_eur","preisänderung_pct",
            "living_area_m2","plot_area_m2","rooms","price_per_m2","year_built",
            "angebotstage","source_key","provider","first_seen","last_seen","url"
        ]
        st.dataframe(view[cols], use_container_width=True, hide_index=True)

        b1, b2 = st.columns(2)
        b1.download_button(
            "CSV herunterladen",
            view.to_csv(index=False).encode("utf-8-sig"),
            "immobilienangebote.csv","text/csv",use_container_width=True
        )
        b2.download_button(
            "Excel inkl. Historie",
            excel_bytes(view, hist, runs),
            "immobilien_monitor_v3.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

elif page == "Import":
    st.subheader("CSV / Excel importieren")
    source_key = st.text_input("Quellenname", placeholder="z. B. Marktbeobachtung Wangen EFH")
    complete = st.checkbox("Vollständiger Snapshot dieser Suche/Quelle")
    upload = st.file_uploader("CSV oder Excel", type=["csv","xlsx","xls"])

    if upload and source_key:
        try:
            raw = pd.read_csv(upload, sep=None, engine="python") if upload.name.lower().endswith(".csv") else pd.read_excel(upload)
            st.dataframe(raw.head(20), use_container_width=True, hide_index=True)
            items = normalize_dataframe(raw, source_key.strip())
            if st.button("Import starten", type="primary"):
                rid = start_run(source_key.strip(), "Datei-Komplettabgleich" if complete else "Dateiimport")
                seen, errors = [], 0
                for item in items:
                    try:
                        upsert_listing(item, rid)
                        seen.append(item["id"])
                    except Exception:
                        errors += 1
                if complete and seen:
                    mark_missing_inactive(source_key.strip(), seen)
                finish_run(rid, len(items), len(seen), errors)
                st.success(f"{len(seen)} Angebote gespeichert/aktualisiert.")
        except Exception as e:
            st.error(f"Importfehler: {e}")

elif page == "Webquellen":
    st.subheader("Erlaubte Websites / Sitemaps")
    st.warning(
        "Nur Quellen verwenden, bei denen automatisierter Abruf erlaubt ist. "
        "Die App umgeht keine Logins, Captchas, Anti-Bot-Sperren oder robots.txt."
    )
    mode = st.radio("Modus", ["Einzelne Angebots-URLs", "Sitemap prüfen"], horizontal=True)
    source_key = st.text_input("Quellenname")
    permission = st.checkbox("Ich bin zum automatisierten Abruf dieser Quelle berechtigt.")

    if mode == "Einzelne Angebots-URLs":
        urls_text = st.text_area("Eine URL pro Zeile", height=180)
        if st.button("Abrufen", type="primary", disabled=not (source_key and permission)):
            urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
            rid = start_run(source_key.strip(), "URL-Liste")
            seen, errors = [], []
            for url in urls:
                try:
                    html, final = safe_get(url)
                    item = extract_listing_from_html(html, final, source_key.strip(), "Einzel-URL")
                    if not item:
                        raise ValueError("Keine eindeutigen Angebotsdaten erkannt.")
                    upsert_listing(item, rid)
                    seen.append(item["id"])
                except Exception as e:
                    errors.append((url, str(e)))
            finish_run(rid, len(urls), len(seen), len(errors))
            st.success(f"{len(seen)} Angebote gespeichert.")
            if errors:
                st.dataframe(pd.DataFrame(errors, columns=["URL","Fehler"]), use_container_width=True, hide_index=True)
    else:
        sitemap = st.text_input("Sitemap-URL", placeholder="https://www.beispiel.de/sitemap.xml")
        max_urls = st.slider("Max. URLs", 10, 500, 100, 10)
        if st.button("Sitemap prüfen", type="primary", disabled=not (source_key and permission and sitemap)):
            rid = start_run(source_key.strip(), "Sitemap-Komplettabgleich")
            urls = read_sitemap_urls(sitemap, max_urls=max_urls)
            seen, errors = [], []
            for url in urls:
                try:
                    html, final = safe_get(url)
                    item = extract_listing_from_html(html, final, source_key.strip(), "Sitemap")
                    if item:
                        upsert_listing(item, rid)
                        seen.append(item["id"])
                except Exception as e:
                    errors.append((url, str(e)))
            if seen:
                mark_missing_inactive(source_key.strip(), seen)
            finish_run(rid, len(urls), len(seen), len(errors))
            st.success(f"{len(urls)} Seiten geprüft, {len(seen)} Angebote erkannt.")
            if errors:
                st.dataframe(pd.DataFrame(errors[:100], columns=["URL","Fehler"]), use_container_width=True, hide_index=True)

elif page == "Preisverlauf":
    st.subheader("Preisverlauf einzelner Angebote")
    if data.empty or hist.empty:
        st.info("Noch keine Historie vorhanden.")
    else:
        labels = data[["id","title","city","source_key"]].copy()
        labels["label"] = labels["title"].fillna("") + " | " + labels["city"].fillna("") + " | " + labels["source_key"].fillna("")
        choice = st.selectbox("Angebot", labels["label"].tolist())
        lid = labels.loc[labels["label"] == choice, "id"].iloc[0]
        one = hist[hist["listing_id"] == lid].sort_values("observed_at")
        st.dataframe(one[["observed_at","price_eur","living_area_m2","plot_area_m2","rooms","status","url"]],
                     use_container_width=True, hide_index=True)
        prices = one[["observed_at","price_eur"]].dropna()
        if len(prices) >= 2:
            chart = prices.copy()
            chart["observed_at"] = pd.to_datetime(chart["observed_at"], utc=True)
            chart = chart.set_index("observed_at")["price_eur"]
            st.line_chart(chart)

elif page == "Quellenverwaltung":
    st.subheader("Automatisch überwachte Quellen")
    st.caption("Diese Liste kann von einem Cloud-Scheduler (z. B. GitHub Actions) automatisch abgearbeitet werden.")
    s1, s2 = st.columns(2)
    source_key = s1.text_input("Name der Quelle", placeholder="Makler Beispiel – Wohnangebote")
    source_type = s2.selectbox("Quellentyp", ["sitemap", "url"])
    source_url = st.text_input("URL")
    max_urls = st.slider("Max. URLs pro Lauf", 1, 500, 100)
    complete = st.checkbox("Als vollständigen Snapshot behandeln", value=True)
    enabled = st.checkbox("Automatische Überwachung aktiv", value=True)
    permission = st.checkbox("Automatisierter Abruf ist für diese Quelle erlaubt.")

    if st.button("Quelle speichern", type="primary", disabled=not (source_key and source_url and permission)):
        save_source(source_key.strip(), source_type, source_url.strip(), enabled, complete, max_urls)
        st.success("Quelle gespeichert.")
        st.rerun()

    sdf = sources_df()
    if not sdf.empty:
        st.dataframe(sdf, use_container_width=True, hide_index=True)
        options = {f"{r['source_key']} ({r['source_type']})": r["source_id"] for _, r in sdf.iterrows()}
        sel = st.selectbox("Quelle löschen", [""] + list(options.keys()))
        if sel and st.button("Ausgewählte Quelle löschen"):
            delete_source(options[sel])
            st.success("Quelle gelöscht.")
            st.rerun()

elif page == "Einstellungen":
    st.subheader("System")
    st.write("Datenbank:", "Cloud/PostgreSQL" if "postgres" in os.getenv("DATABASE_URL","").lower() else "SQLite/Fallback")
    st.write("Login:", "aktiv" if get_secret("APP_PASSWORD","") else "nicht konfiguriert")
    st.write("Gespeicherte Angebote:", len(data))
    st.write("Abrufläufe:", len(runs))

    st.divider()
    st.markdown("#### Portal-Hinweis")
    st.info(
        "Für große Immobilienportale bitte nur offizielle, für deinen Anwendungsfall freigegebene APIs, "
        "Datenfeeds oder eigene Exporte verwenden. Diese App enthält keine Umgehung von Zugangsbeschränkungen."
    )

    st.divider()
    confirm = st.checkbox("Alle Angebots- und Verlaufsdaten wirklich löschen")
    if st.button("Alle Daten löschen", disabled=not confirm):
        clear_all()
        st.success("Daten gelöscht.")
        st.rerun()
