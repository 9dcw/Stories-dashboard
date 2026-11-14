"""Exercise a large feed without touching server records."""
from pathlib import Path
import copy
import sys

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from api.candidate_page import paginate_candidates
model = {
    "recent_candidates": [dict(item_id=i, headline=f"Candidate {i:05d}", source="Court",
                               date="2026-11-14", lane="B", status="NEW", summary_status="COMPLETE") for i in range(4631)],
    "stories": [], "sources": [], "source_proposals": [],
}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width in (1280, 390):
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            from urllib.parse import urlparse, parse_qs
            path = urlparse(route.request.url).path
            if route.request.url.startswith("https://accounts.google.com"):
                route.abort()
            elif path == "/api/v1/config":
                route.fulfill(json={"google_client_id": "test"})
            elif path == "/api/v1/auth/session":
                route.fulfill(json={"csrf_token": "test"})
            elif path == "/api/v1/dashboard":
                params = {k: v[0] for k, v in parse_qs(urlparse(route.request.url).query, keep_blank_values=True).items()}
                filters = {k: params.get(k, "") for k in ("source", "lane", "screening", "dismissed", "problem")}
                route.fulfill(json=paginate_candidates(copy.deepcopy(model), view=params.get("view", "candidates"),
                              query=params.get("query", ""), filters=filters, limit=int(params["page_size"]),
                              cursor=params.get("cursor", "")))
            else:
                file = root / (path.lstrip("/") or "index.html")
                route.fulfill(path=str(file)) if file.is_file() else route.fulfill(status=404, body="Not found")

        page.route("**/*", handle)
        page.goto("https://stories.test/")
        page.locator("#candidateGrid .candidate-card").nth(49).wait_for()
        assert page.locator("#candidateGrid .candidate-card").count() == 50
        page.locator("#moreCandidates").click()
        expect(page.locator("#candidateGrid .candidate-card")).to_have_count(100)
        page.locator("#searchInput").fill("Candidate 00000")
        expect(page.locator("#candidateGrid .candidate-card")).to_have_count(1)
        assert page.locator("#moreCandidates").is_hidden()
        assert "Candidate 00000" in page.locator("#candidateGrid").inner_text()
        assert not errors, errors
        page.close()
    browser.close()
print("Large-feed loading, show more, and full-feed search passed on desktop and mobile")
