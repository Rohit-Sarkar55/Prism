"""The ops console page and the /admin/keys endpoint that feeds it."""

import re

from app.config import PROJECT_ROOT

SEARCH = "prism-sk-search-1a2b3c"
BUDGET = "prism-sk-budget-demo-0j1k2l"
ADMIN = {"Authorization": "Bearer demo-admin-token"}
PAGE = (PROJECT_ROOT / "app" / "static" / "console.html").read_text(encoding="utf-8")
BODY = {"model": "fast", "messages": [{"role": "user", "content": "What is a load balancer?"}]}


# ---- the page -------------------------------------------------------------------------------------


async def test_the_console_loads_without_a_token_and_with_a_strict_per_request_policy(client):
    first = await client.get("/console")
    second = await client.get("/console")
    assert first.status_code == 200 and first.headers["content-type"].startswith("text/html")
    assert first.headers["cache-control"] == "no-store" and first.headers["x-content-type-options"] == "nosniff"

    csp = first.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([\w-]+)'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp and "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert first.text.count(f'nonce="{nonce}"') == 2  # exactly its own script and style
    assert "__NONCE__" not in first.text
    assert re.search(r"nonce-([\w-]+)", second.headers["content-security-policy"]).group(1) != nonce


async def test_the_console_pulls_in_nothing_from_outside(client):
    body = (await client.get("/console")).text
    assert "http://" not in body and "https://" not in body  # no CDN, no fonts, no trackers
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", body, re.IGNORECASE)


def test_the_page_never_writes_api_data_as_html():
    # Request logs hold text that callers control (the model name they sent). Rendering it as HTML in an
    # admin page would be a stored XSS hole, so the page may only build its DOM from text nodes.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in PAGE, forbidden


def test_the_page_uses_the_admin_api_and_keeps_the_token_out_of_urls():
    for path in ("/admin/keys", "/admin/logs", "/admin/cache/stats", "/admin/providers"):
        assert path in PAGE
    assert "Authorization" in PAGE and "token=" not in PAGE and "localStorage" not in PAGE  # session only


# ---- /admin/keys ----------------------------------------------------------------------------------


async def test_keys_needs_the_admin_token(client):
    assert (await client.get("/admin/keys")).status_code == 401
    assert (await client.get("/admin/keys", headers={"Authorization": f"Bearer {SEARCH}"})).status_code == 401


async def test_keys_lists_every_tenant_with_limits_and_zero_usage_at_the_start(cache_client):
    # cache_client runs the cache exactly as seeded; the plain client switches it off for every key
    body = (await cache_client.get("/admin/keys", headers=ADMIN)).json()
    assert re.fullmatch(r"\d{4}-\d{2}", body["month"])
    by_team = {k["team"]: k for k in body["keys"]}
    assert set(by_team) == {"search", "research", "free-tier", "budget-demo"}
    assert [k["team"] for k in body["keys"]] == sorted(by_team)  # stable order for the table

    search = by_team["search"]
    assert (search["key"], search["rpm"], search["model_allowlist"]) == (SEARCH, 60, ["fast"])
    assert (search["cache_enabled"], search["cache_threshold"]) == (True, 0.92)
    assert (by_team["research"]["cache_enabled"], by_team["research"]["cache_threshold"]) == (False, None)
    assert by_team["budget-demo"]["monthly_budget_usd"] == 0.00001
    assert all(k["requests"] == 0 and k["spent_nano"] == 0 and k["budget_used_pct"] == 0 for k in body["keys"])


async def test_keys_reports_spend_and_budget_used_and_matches_the_usage_api(client):
    # one request on the default fake: 5 prompt + 5 completion tokens on alpha-small = 3_750 nano-USD
    resp = await client.post("/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {BUDGET}"})
    assert resp.status_code == 200

    keys = {k["team"]: k for k in (await client.get("/admin/keys", headers=ADMIN)).json()["keys"]}
    demo = keys["budget-demo"]
    assert (demo["requests"], demo["spent_nano"], demo["prompt_tokens"], demo["completion_tokens"]) == (1, 3750, 5, 5)
    assert demo["budget_used_pct"] == 37.5  # 3_750 of a 10_000 nano-USD budget
    assert demo["spent_usd"] == 0.00000375
    assert keys["search"]["requests"] == 0  # another key's spend is untouched

    usage = (await client.get("/admin/usage", params={"key": BUDGET}, headers=ADMIN)).json()
    assert usage["cost_nano"] == demo["spent_nano"] and usage["requests"] == demo["requests"]


async def test_budget_used_can_exceed_one_hundred_percent_because_overshoot_is_documented(client, fakes):
    fakes["alpha"].reply = " ".join(["word"] * 20)  # 12_750 nano against a 10_000 budget
    await client.post("/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {BUDGET}"})
    demo = next(
        k for k in (await client.get("/admin/keys", headers=ADMIN)).json()["keys"] if k["team"] == "budget-demo"
    )
    assert demo["budget_used_pct"] == 127.5


# ---- the admin token check must never crash -------------------------------------------------------


async def test_a_non_ascii_admin_token_is_a_401_not_a_500(client):
    # compare_digest on two str values raises TypeError for non-ASCII input; the check compares bytes
    for path in ("/admin/keys", "/admin/logs", "/admin/providers", "/admin/cache/stats"):
        resp = await client.get(path, headers=[(b"authorization", "Bearer caf\u00e9".encode())])
        assert resp.status_code == 401, path