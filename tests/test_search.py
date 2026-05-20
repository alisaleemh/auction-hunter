from search import filter_and_sort_results, normalize_text, query_tokens, relevance_score


def _result(title: str, description: str = "", condition: str = "") -> dict:
    return {
        "source": "HiBid",
        "auction_title": "Auction",
        "auction_address": "20 Automatic Rd, Brampton, ON",
        "distance_miles": 3.2,
        "lot_title": title,
        "lot_number": "1",
        "current_bid": 5,
        "end_time": "",
        "end_time_iso": "",
        "shipping_available": True,
        "condition": condition,
        "description": description,
        "details": "",
        "url": "https://example.com/lot/1",
    }


def test_normalize_and_tokenize_query():
    assert normalize_text(" Baby, Stair-Gate!! ") == "baby stair gate"
    assert query_tokens(" Baby, Stair-Gate!! ") == ["baby", "stair", "gate"]


def test_single_word_query_matches():
    results = filter_and_sort_results([_result("Baby Gate")], "gate")
    assert len(results) == 1


def test_multi_word_query_matches_across_fields():
    results = filter_and_sort_results(
        [_result("Baby Item", description="Pressure mounted stair gate for hallway")],
        "baby stair gate",
    )
    assert len(results) == 1


def test_punctuation_and_case_do_not_block_match():
    results = filter_and_sort_results(
        [_result("BABY stair-gate", condition="Open Box")],
        "baby stair gate",
    )
    assert len(results) == 1


def test_no_match_returns_empty():
    assert filter_and_sort_results([_result("Dining Chair")], "baby stair gate") == []


def test_title_exact_match_sorts_before_description_only_match():
    description_only = _result("Baby Item", description="stair gate with hardware")
    title_match = _result("Baby Stair Gate", description="hardware included")
    results = filter_and_sort_results([description_only, title_match], "baby stair gate")
    assert results[0]["lot_title"] == "Baby Stair Gate"


def test_typo_query_still_matches():
    results = filter_and_sort_results([_result("Baby Stair Gate", description="Pressure mounted safety gate")], "babby stare gte")
    assert len(results) == 1


def test_contextual_query_matches_item_text():
    results = filter_and_sort_results(
        [_result("Graco 4Ever Car Seat", description="rear facing infant convertible booster child seat")],
        "baby carseat",
    )
    assert len(results) == 1


def test_irrelevant_result_stays_filtered_out():
    score = relevance_score(_result("Dining Table", description="wood kitchen furniture"), "baby gate")
    assert score < 0.62
