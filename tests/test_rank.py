"""候选排序测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.rank import rank_candidates  # noqa: E402
from engines.base import Candidate  # noqa: E402


def candidate(url, **kwargs):
    return Candidate(url=url, **kwargs)


class TestRank(unittest.TestCase):
    def test_verified_pixiv_beats_lens_x(self):
        pixiv = candidate(
            "https://www.pixiv.net/artworks/100000000",
            platform="Pixiv",
            author="画师A",
            image="https://i.pximg.net/a.jpg",
            engine="SauceNAO",
            similarity=96,
            exact=True,
            verified=True,
        )
        x = candidate(
            "https://x.com/artist/status/123",
            platform="X",
            author="artist",
            engine="Google Lens",
            exact=True,
        )
        pinterest = candidate(
            "https://www.pinterest.com/pin/1/",
            platform="Pinterest",
            engine="Yandex",
            exact=True,
        )
        ranked = rank_candidates([x, pinterest, pixiv], limit=2)
        self.assertIs(ranked.primary, pixiv)
        self.assertTrue(ranked.primary.verified)
        self.assertEqual([c.platform for c in ranked.alternates], ["X"])

    def test_dedup_same_image_counts_mirrors(self):
        primary = candidate(
            "https://x.com/a/status/1",
            platform="X",
            author="a",
            image="https://pbs.twimg.com/media/ABC.jpg",
            engine="Google Lens",
            exact=True,
        )
        mirror = candidate(
            "https://example.com/copy",
            platform="example.com",
            image="https://pbs.twimg.com/media/ABC.jpg",
            engine="Yandex",
            exact=True,
        )
        ranked = rank_candidates([mirror, primary], limit=3)
        self.assertEqual(len(ranked.alternates), 0)
        self.assertEqual(ranked.primary.extra.get("mirrors"), 1)

    def test_listing_pages_dropped(self):
        primary = candidate(
            "https://www.pixiv.net/artworks/1",
            platform="Pixiv",
            author="a",
            engine="SauceNAO",
            exact=True,
            verified=True,
        )
        listing = candidate(
            "https://danbooru.donmai.us/posts?page=1&tags=raimon",
            platform="Danbooru",
            engine="Yandex",
            exact=True,
            verified=True,
        )
        ranked = rank_candidates([primary, listing], limit=3)
        self.assertEqual(len(ranked.alternates), 0)

    def test_low_quality_alternative_dropped(self):
        primary = candidate(
            "https://www.pixiv.net/artworks/1",
            platform="Pixiv",
            author="a",
            engine="SauceNAO",
            exact=True,
            verified=True,
        )
        forum = candidate(
            "https://warosu.org/jp/thread/1",
            platform="warosu.org",
            engine="Yandex",
            exact=True,
            verified=True,
        )
        ranked = rank_candidates([primary, forum], limit=3)
        self.assertEqual(ranked.alternates, [])

    def test_mismatch_is_demoted(self):
        verified = candidate(
            "https://x.com/a/status/1",
            platform="X",
            author="a",
            image="https://pbs.twimg.com/1.jpg",
            engine="Yandex",
            exact=True,
            verified=True,
        )
        mismatch = candidate(
            "https://www.pixiv.net/artworks/2",
            platform="Pixiv",
            author="b",
            image="https://i.pximg.net/2.jpg",
            engine="Yandex",
            exact=False,
            extra={"verify": "mismatch"},
        )
        ranked = rank_candidates([mismatch, verified], limit=3)
        self.assertIs(ranked.primary, verified)


if __name__ == "__main__":
    unittest.main()
