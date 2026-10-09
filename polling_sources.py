"""Representative Stage 2 source registry entries."""

STAGE2_SOURCES = [
    ("DOJ press releases", "A", "US", "https://www.justice.gov/news/press-releases", "rss", "https://www.justice.gov/news/rss?type=press_release&m=1", "{}"),
    ("FBI news", "A", "US", "https://www.fbi.gov/news/press-releases", "rss", "https://www.fbi.gov/feeds/national-press-releases/atom.xml", '{"allow_archive_segments":["news"]}'),
    ("SEC litigation releases", "A", "US", "https://www.sec.gov/litigation/litreleases", "rss", "https://www.sec.gov/news/pressreleases.rss", "{}"),
    ("New Jersey DOBI press releases", "C", "NJ", "https://www.nj.gov/dobi/pressreleases/2026.html", "html_list", "https://www.nj.gov/dobi/pressreleases/2026.html", '{"allowed_path_prefixes":["/dobi/pressreleases/pr"]}'),
    ("New York DFS press releases", "C", "NY", "https://www.dfs.ny.gov/reports_and_publications/press_releases", "html_list", "https://www.dfs.ny.gov/reports_and_publications/press_releases", '{"allowed_path_prefixes":["/reports_and_publications/press_releases/pr"]}'),
    ("California DOI news", "C", "CA", "https://www.insurance.ca.gov/0400-news/0100-press-releases/2026/", "html_list", "https://www.insurance.ca.gov/0400-news/0100-press-releases/2026/", '{"allowed_path_prefixes":["/0400-news/0100-press-releases/2026/"]}'),
    ("NAIC newsroom", "C", "US", "https://content.naic.org/newsroom", "html_list", "https://content.naic.org/newsroom", '{"allowed_path_prefixes":["/newsroom"]}'),
    ("CourtListener opinions", "B", "US", "https://www.courtlistener.com/feed/court/all/", "rss", "https://www.courtlistener.com/feed/court/all/", "{}"),
    ("U.S. Supreme Court opinions", "B", "US", "https://www.supremecourt.gov/opinions/slipopinion.aspx", "html_list", "https://www.supremecourt.gov/opinions/slipopinion.aspx", '{"allowed_path_prefixes":["/opinions/"]}'),
    ("Insurance Journal", "D", "US", "https://www.insurancejournal.com/news/", "rss", "https://www.insurancejournal.com/rss/news/", '{"allow_archive_segments":["news"]}'),
]
