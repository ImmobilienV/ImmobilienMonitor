
import os
from db_core import init_db, sources_df, start_run, upsert_listing, finish_run, mark_missing_inactive
from scraping_core import safe_get, extract_listing_from_html, read_sitemap_urls

def run_source(row):
    source_key = row["source_key"]
    source_type = row["source_type"]
    source_url = row["source_url"]
    max_urls = int(row.get("max_urls", 100) or 100)

    run_id = start_run(source_key, f"Scheduled:{source_type}")
    seen, errors, found = [], 0, 0

    try:
        if source_type == "sitemap":
            urls = read_sitemap_urls(source_url, max_urls=max_urls)
        elif source_type == "url":
            urls = [source_url]
        else:
            finish_run(run_id, 0, 0, 1)
            return

        found = len(urls)
        for url in urls:
            try:
                html, final = safe_get(url)
                item = extract_listing_from_html(html, final, source_key, "Scheduled Scan")
                if item:
                    upsert_listing(item, run_id)
                    seen.append(item["id"])
            except Exception as e:
                errors += 1
                print(f"[ERROR] {source_key} {url}: {e}")

        if int(row.get("complete_snapshot", 0) or 0) and seen:
            mark_missing_inactive(source_key, seen)
        finish_run(run_id, found, len(seen), errors)
    except Exception as e:
        print(f"[ERROR] source {source_key}: {e}")
        finish_run(run_id, found, len(seen), errors + 1)

def main():
    init_db()
    df = sources_df()
    if df.empty:
        print("No sources configured.")
        return
    for _, row in df[df["enabled"] == 1].iterrows():
        run_source(row.to_dict())

if __name__ == "__main__":
    main()
