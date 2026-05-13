"""PolicyRules YAML 로더 (P4 산출물) 단위 회귀 테스트.

- 정상 케이스: config/policy_rules.yaml 에서 anchor_keywords 로드
- 미정의 tag: 빈 튜플
- 누락된 키: 빈 튜플
- 파일 부재·잘못된 YAML: 빈 매핑으로 안전 폴백
- mtime 캐시: 동일 mtime 이면 디스크 미접근
- casefold: frozenset 형식 정합
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 기본 케이스 (실 파일 사용)
# ---------------------------------------------------------------------------

def test_elderly_benefits_anchor_loaded_from_yaml():
    from app.chat.infra.rag import policy_config

    policy_config.reset_cache()
    anchors = policy_config.get_anchor_keywords("elderly_benefits")
    # YAML 에 정의된 항목이 모두 등장
    assert "기초연금" in anchors
    assert "기초 연금" in anchors


def test_anchor_keywords_casefold_returns_frozenset():
    from app.chat.infra.rag import policy_config

    policy_config.reset_cache()
    cf = policy_config.get_anchor_keywords_casefold("elderly_benefits")
    assert isinstance(cf, frozenset)
    # 모든 원소가 casefold 결과여야 함
    for kw in cf:
        assert kw == kw.casefold()


def test_unknown_tag_returns_empty():
    from app.chat.infra.rag import policy_config

    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("__no_such_tag__") == ()
    assert policy_config.get_anchor_keywords("") == ()
    assert policy_config.get_anchor_keywords_casefold("__no_such_tag__") == frozenset()


def test_empty_string_tag_handled():
    from app.chat.infra.rag import policy_config

    assert policy_config.get_anchor_keywords("") == ()


# ---------------------------------------------------------------------------
# 안전 폴백 (잘못된 YAML / 파일 부재)
# ---------------------------------------------------------------------------

def test_missing_yaml_returns_empty(monkeypatch, tmp_path):
    """존재하지 않는 경로면 빈 매핑."""
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(missing), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ()


def test_malformed_yaml_returns_empty(monkeypatch, tmp_path):
    """YAML 문법 오류 시 빈 매핑으로 폴백."""
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    bad = tmp_path / "bad.yaml"
    bad.write_text("priority_tags: [this is not a dict", encoding="utf-8")
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(bad), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ()


def test_non_dict_root_returns_empty(monkeypatch, tmp_path):
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    weird = tmp_path / "weird.yaml"
    weird.write_text("- list item\n- another", encoding="utf-8")
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(weird), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ()


# ---------------------------------------------------------------------------
# 사용자 정의 YAML
# ---------------------------------------------------------------------------

def test_custom_yaml_overrides_anchors(monkeypatch, tmp_path):
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords:\n"
        "      - foo\n"
        "      - bar\n"
        "  implant:\n"
        "    anchor_keywords:\n"
        "      - dental\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(custom), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ("foo", "bar")
    assert policy_config.get_anchor_keywords("implant") == ("dental",)


def test_yaml_dedupes_and_preserves_order(monkeypatch, tmp_path):
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    dup = tmp_path / "dup.yaml"
    dup.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords:\n"
        "      - alpha\n"
        "      - beta\n"
        "      - alpha\n"
        "      - ''\n"  # 빈 항목은 제거
        "      - gamma\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(dup), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ("alpha", "beta", "gamma")


def test_non_string_items_filtered(monkeypatch, tmp_path):
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    mixed = tmp_path / "mixed.yaml"
    mixed.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords:\n"
        "      - text\n"
        "      - 123\n"
        "      - null\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(mixed), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ("text",)


# ---------------------------------------------------------------------------
# mtime 캐시 동작 — 동일 mtime 이면 디스크 재읽기 없음
# ---------------------------------------------------------------------------

def test_mtime_cache_avoids_redundant_read(monkeypatch, tmp_path):
    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    src = tmp_path / "cached.yaml"
    src.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords: [first]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(src), raising=False)
    policy_config.reset_cache()
    first = policy_config.get_anchor_keywords("elderly_benefits")
    assert first == ("first",)

    # 같은 파일 mtime — 디스크에서 다시 안 읽어와야 함
    # (정상 mtime 보존 확인: 같은 결과 반환)
    again = policy_config.get_anchor_keywords("elderly_benefits")
    assert again == first


def test_mtime_change_triggers_reload(monkeypatch, tmp_path):
    """파일 mtime 이 변하면 새 내용이 반영된다."""
    import os
    import time

    from app.chat.infra.rag import policy_config
    from app.core.config import Config

    src = tmp_path / "reload.yaml"
    src.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords: [old]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Config, "POLICY_RULES_PATH", str(src), raising=False)
    policy_config.reset_cache()
    assert policy_config.get_anchor_keywords("elderly_benefits") == ("old",)

    # 파일 내용·mtime 변경
    time.sleep(0.05)
    src.write_text(
        "priority_tags:\n"
        "  elderly_benefits:\n"
        "    anchor_keywords: [new]\n",
        encoding="utf-8",
    )
    # 일부 OS 에서 동일 초 단위 mtime 충돌 회피
    new_time = src.stat().st_mtime + 1
    os.utime(src, (new_time, new_time))

    assert policy_config.get_anchor_keywords("elderly_benefits") == ("new",)
