from __future__ import annotations

import json
import random
from typing import Any

from garmin_archive.devtools.sanitize import (
    REDACTED,
    REDACTED_URL,
    SanitizeContext,
    check_leaks,
    sanitize,
    sensitive_set,
)


def make_ctx() -> SanitizeContext:
    return SanitizeContext(rng=random.Random(1234))


def sample() -> dict[str, Any]:
    return {
        "activityId": 12345678901,
        "activityName": "Morning Run with Bob",
        "description": "secret notes",
        "ownerDisplayName": "Jane Doe",
        "activityLikeDisplayNames": ["Jane D", "Bobby T"],
        "startTimeGMT": "2024-05-01T06:30:00Z",
        "startLatitude": 51.5,
        "endLongitude": -0.12,
        "userProfileId": 99887766,
        "childIds": [12345678902, 12345678903],
        "parentId": None,
        "profileImageUrl": "https://example.com/pic.jpg",
        "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
        "eventType": {"typeId": 9, "typeKey": "uncategorized"},
        "distance": 1000.5,
    }


def test_catalog_sub_objects_kept() -> None:
    out = sanitize(sample(), make_ctx())
    assert out["activityType"] == {"typeId": 1, "typeKey": "running", "parentTypeId": 17}
    assert out["eventType"]["typeKey"] == "uncategorized"


def test_identifiers_pseudonymized_consistently() -> None:
    ctx = make_ctx()
    a = sanitize({"activityId": 12345678902, "x": {"childIds": [12345678902]}}, ctx)
    assert a["activityId"] == a["x"]["childIds"][0]
    assert a["activityId"] != 12345678902
    assert isinstance(a["activityId"], int)


def test_links_consistent_across_documents() -> None:
    ctx = make_ctx()
    parent = sanitize({"activityId": 12345678901, "childIds": [12345678902]}, ctx)
    child = sanitize({"activityId": 12345678902, "parentId": 12345678901}, ctx)
    assert parent["childIds"][0] == child["activityId"]
    assert child["parentId"] == parent["activityId"]


def test_digit_count_preserved_and_never_equal() -> None:
    for seed in range(50):
        ctx = SanitizeContext(rng=random.Random(seed))
        for orig in (123456, 12345678901, 9876543210123):
            out = sanitize({"activityId": orig}, ctx)["activityId"]
            assert out != orig
            assert len(str(out)) == len(str(orig))


def test_string_numeric_id_stays_string() -> None:
    out = sanitize({"activityId": "12345678901"}, make_ctx())["activityId"]
    assert isinstance(out, str)
    assert out != "12345678901"
    assert len(out) == 11


def test_uuid_pseudonym_is_uuid() -> None:
    orig = "123e4567-e89b-42d3-a456-426614174000"
    out = sanitize({"activityUUID": orig}, make_ctx())["activityUUID"]
    assert out != orig
    assert len(out) == 36


def test_free_text_and_urls_redacted() -> None:
    out = sanitize(sample(), make_ctx())
    assert out["activityName"] == REDACTED
    assert out["description"] == REDACTED
    assert out["ownerDisplayName"] == REDACTED
    assert out["activityLikeDisplayNames"] == [REDACTED, REDACTED]
    assert out["profileImageUrl"] == REDACTED_URL


def test_coordinates_nulled() -> None:
    out = sanitize(sample(), make_ctx())
    assert out["startLatitude"] is None
    assert out["endLongitude"] is None


def test_unchanged_fields() -> None:
    out = sanitize(sample(), make_ctx())
    assert out["startTimeGMT"] == "2024-05-01T06:30:00Z"
    assert out["distance"] == 1000.5
    assert out["parentId"] is None


def test_redaction_log_holds_paths_not_values() -> None:
    ctx = make_ctx()
    sanitize(sample(), ctx, source="s")
    assert ctx.redaction_log
    dumped = repr(ctx.redaction_log)
    assert "Jane Doe" not in dumped
    assert "12345678901" not in dumped
    assert any(e.json_path == "$.activityName" and e.rule == "free_text" for e in ctx.redaction_log)


def test_sanitize_does_not_mutate_input() -> None:
    data = sample()
    snapshot = json.dumps(data, sort_keys=True)
    sanitize(data, make_ctx())
    assert json.dumps(data, sort_keys=True) == snapshot


def test_sanitized_output_has_no_leaks() -> None:
    ctx = make_ctx()
    out = sanitize(sample(), ctx, source="s")
    assert check_leaks(json.dumps(out), sensitive_set(ctx, "s")) == []


def test_planted_original_detected_path_only() -> None:
    ctx = make_ctx()
    sanitize({"activityName": "Morning Run with Bob"}, ctx, source="s")
    bad = json.dumps({"a": {"b": ["xx Morning Run with Bob xx"]}})
    hits = check_leaks(bad, sensitive_set(ctx, "s"))
    assert [h.json_path for h in hits] == ["$.a.b[0]"]
    assert not hasattr(hits[0], "value")


def test_typekey_equal_name_excluded() -> None:
    ctx = make_ctx()
    doc = {"activityType": {"typeKey": "running"}, "activityName": "running"}
    sanitize(doc, ctx, source="s")
    assert "running" not in sensitive_set(ctx, "s")


def test_pass_through_union_when_source_none() -> None:
    ctx = make_ctx()
    sanitize({"activityType": {"typeKey": "running"}}, ctx, source="a")
    sanitize({"activityName": "running"}, ctx, source="b")
    assert "running" in sensitive_set(ctx, "b")
    assert "running" not in sensitive_set(ctx, None)


def test_numeric_digit_boundary() -> None:
    sens = {"12345678"}
    assert check_leaks('{"a": "x123456789"}', sens) == []
    assert check_leaks('{"a": "912345678"}', sens) == []
    assert [h.json_path for h in check_leaks('{"a": 12345678}', sens)] == ["$.a"]
    assert [h.json_path for h in check_leaks('{"a": "id-12345678-z"}', sens)] == ["$.a"]


def test_min_lengths() -> None:
    ctx = make_ctx()
    ctx.originals.update({"abcd", "abc", "1234", "12345"})
    s = sensitive_set(ctx, "s")
    assert "abcd" in s
    assert "abc" not in s
    assert "1234" not in s
    assert "12345" in s


def test_non_json_returns_root_hit() -> None:
    hits = check_leaks("plain text with SecretName", {"SecretName"})
    assert [h.json_path for h in hits] == ["$"]


def test_leak_in_dict_key_located() -> None:
    hits = check_leaks('{"outer": {"SecretName": 1}}', {"SecretName"})
    assert [h.json_path for h in hits] == ["$.outer.SecretName"]


def test_clean_text_no_hits() -> None:
    assert check_leaks('{"a": 1}', {"SecretName"}) == []
