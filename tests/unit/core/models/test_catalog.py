from core.models.catalog import normalize_degraded_stages


def test_normalize_degraded_stages_ignores_non_string_candidates() -> None:
    stages = ["caption", ["contextualize"], {"topic_tag": True}, 7, "unknown", "topic_tag"]

    assert normalize_degraded_stages(stages) == ["caption", "topic_tag"]
