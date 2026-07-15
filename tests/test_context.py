from umbra_py import llm_context
from umbra_py.constants import PRODUCT_ASSETS


def test_llm_context_covers_every_product_type():
    ctx = llm_context()
    # Every canonical product type has a plain-language explanation.
    for name in PRODUCT_ASSETS:
        assert name in ctx["product_types"]
        assert ctx["product_types"][name]


def test_llm_context_documents_search_and_license():
    ctx = llm_context()
    # Search parameters an agent needs to build a query.
    assert {"bbox", "place", "area", "start", "end"} <= set(ctx["search_parameters"])
    # License rules are explicit and required.
    assert ctx["license"]["data_license"] == "CC-BY-4.0"
    assert ctx["license"]["attribution_required"] is True


def test_llm_context_is_json_serialisable():
    import json

    # An agent consumes this over a wire; it must round-trip through JSON.
    assert json.loads(json.dumps(llm_context())) == llm_context()
