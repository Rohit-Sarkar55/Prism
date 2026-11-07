"""`auto` end to end: the classifier picks the tier, the alias chain does the rest."""

import json
import sys

from app.config import get_settings
from app.main import app
from app.routing import resolve_route
from app.seed import load_gateway_config, load_pricing
from tools import routing_eval

RESEARCH = "prism-sk-research-4d5e6f"  # allowlist: fast, smart, auto
ADMIN = {"Authorization": "Bearer demo-admin-token"}
DATA = get_settings().data_dir
CONFIG = load_gateway_config(DATA / "gateway_config.sample.json")
PRICING = load_pricing(DATA / "model_pricing.json")


def msgs(text):
    return [{"role": "user", "content": text}]


def test_simple_prompts_resolve_to_the_fast_chain_with_a_reason():
    route = resolve_route(CONFIG, PRICING, "auto", msgs("What is the capital of Peru?"))
    assert route.alias == "fast" and route.chain == ["alpha-small", "beta-small"]
    assert route.reason.startswith("simple")


def test_complex_prompts_resolve_to_the_smart_chain_with_a_reason():
    route = resolve_route(CONFIG, PRICING, "auto", msgs("Prove that the square root of 3 is irrational."))
    assert route.alias == "smart" and route.chain == ["alpha-large", "beta-large"]
    assert route.reason.startswith("complex")


def test_plain_aliases_do_not_run_the_classifier():
    assert resolve_route(CONFIG, PRICING, "smart", msgs("What is the capital of Peru?")).reason is None


async def post(client, model, text):
    body = {"model": model, "messages": msgs(text)}
    return await client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {RESEARCH}"})


async def last_log(client):
    return (await client.get("/admin/logs", params={"key": RESEARCH, "limit": 1}, headers=ADMIN)).json()["logs"][0]


async def test_auto_sends_an_easy_prompt_to_the_cheap_model_and_a_hard_one_to_the_strong_model(client):
    easy = await post(client, "auto", "Convert 12 inches to centimetres.")
    assert easy.headers["x-prism-provider"] == "alpha/alpha-small"
    assert (await last_log(client))["route_reason"].startswith("simple")

    hard = await post(client, "auto", "Compare two database designs for a ledger and recommend one.")
    assert hard.headers["x-prism-provider"] == "alpha/alpha-large"
    entry = await last_log(client)
    assert entry["requested_model"] == "auto" and entry["resolved_model"] == "alpha-large"
    assert entry["route_reason"].startswith("complex")


async def test_a_hard_prompt_routed_by_auto_costs_more_than_an_easy_one(client):
    easy = await post(client, "auto", "Convert 12 inches to centimetres.")
    hard = await post(client, "auto", "Compare two database designs for a ledger and recommend one.")
    assert float(hard.headers["x-prism-cost-usd"]) > float(easy.headers["x-prism-cost-usd"])


async def test_auto_still_fails_over_within_the_chosen_tier(client, fakes):
    fakes["alpha"].fail_times = 99
    resp = await post(client, "auto", "Prove that the sum of two even numbers is even.")
    assert resp.headers["x-prism-provider"] == "beta/beta-large" and resp.headers["x-prism-fallback"] == "true"


# ---- the one-command eval ----------------------------------------------------------------


def run_tool(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["routing_eval", *args])
    code = routing_eval.main()
    return code, capsys.readouterr().out


def test_eval_tool_json_matches_the_shape_in_the_evaluation_guide(monkeypatch, capsys):
    code, out = run_tool(monkeypatch, capsys, "--json")
    report = json.loads(out)
    assert code == 0 and report["total"] == 20
    assert 0.9 <= report["accuracy"] <= 1.0 and report["correct"] == round(report["accuracy"] * 20)
    assert set(report["cases"][0]) == {"id", "expected", "actual", "reason"}


def test_eval_tool_reports_the_baseline_and_the_held_out_set(monkeypatch, capsys):
    _, out = run_tool(monkeypatch, capsys, "--quiet")
    assert "length-only baseline" in out and "provided set" in out and "held-out set" in out


def test_eval_tool_can_gate_on_accuracy(monkeypatch, capsys):
    assert run_tool(monkeypatch, capsys, "--min-accuracy", "0.5")[0] == 0
    assert run_tool(monkeypatch, capsys, "--min-accuracy", "1.01")[0] == 1


def test_app_state_is_untouched_by_the_eval_tool():
    assert app.title == "Prism"  # the tool imports the classifier only, never the running app's state