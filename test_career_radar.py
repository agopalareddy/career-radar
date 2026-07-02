"""Offline self-checks — run with: python3 test_career_radar.py (no deps needed)."""

import os
import tempfile
from pathlib import Path

from career_radar import (
    build_digest,
    filter_new_posts,
    load_env,
    update_seen,
    SEEN_CAP,
)


def test_filter_new_posts():
    posts = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert filter_new_posts(posts, ["b"]) == [{"id": "a"}, {"id": "c"}]
    assert filter_new_posts(posts, []) == posts
    assert filter_new_posts([], ["b"]) == []


def test_update_seen_caps():
    seen = update_seen([f"old{i}" for i in range(SEEN_CAP)], [{"id": "new"}])
    assert len(seen) == SEEN_CAP
    assert seen[-1] == "new"
    assert "old0" not in seen


def test_build_digest():
    posts = [{
        "id": "x", "subreddit": "resumes", "title": "One-page rule?",
        "selftext": "Is it still a thing", "score": 42, "num_comments": 7,
        "comments": ["Yes, unless 10+ YOE"],
    }]
    digest = build_digest(posts)
    assert "[r/resumes] One-page rule? (42 pts, 7 comments)" in digest
    assert "Is it still a thing" in digest
    assert "> top comment: Yes, unless 10+ YOE" in digest


def test_load_env_parsing_and_no_override():
    os.environ["CR_TEST_EXISTING"] = "keep"
    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text(
            "# comment\n"
            "\n"
            "CR_TEST_NEW=hello world\n"
            'CR_TEST_QUOTED="quoted value"\n'
            "CR_TEST_EXISTING=clobbered\n"
            "not a kv line\n"
        )
        load_env(env)
    assert os.environ["CR_TEST_NEW"] == "hello world"
    assert os.environ["CR_TEST_QUOTED"] == "quoted value"
    assert os.environ["CR_TEST_EXISTING"] == "keep"  # real env wins
    load_env(Path("/nonexistent/.env"))  # missing file is a no-op


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("all checks passed")
