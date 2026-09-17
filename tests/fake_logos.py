"""One small Logos book, served by a fake articles API.

Shared by the unit suite (with the staging tables faked too) and the core
integration suite (with real Postgres behind them), so both drive the same walk
over the same scripts: Greek, pointed Hebrew, English commentary and an index.
"""

from __future__ import annotations

RESOURCE = "LLS:TEST.LEXICON"
TITLE = "A Test Lexicon"

#: Article bodies in the scripts the plugin actually ingests.
ARTICLES = {
    "A.1": "<p>λόγος, ου, ὁ — word, speech; cp. John 1:1.</p>"
    + "<p>πάντα διʼ αὐτοῦ ἐγένετο. </p>" * 40,
    "A.2": "<p>בְּרֵאשִׁית בָּרָא אֱלֹהִים אֵת הַשָּׁמַיִם</p>" * 30,
    "A.2.1": "<p>3:16 For God so loved the world, that he gave his only Son.</p>"
    + "<p>This verse summarises the gospel. </p>" * 60,
    "B.1": "<p>ἀγάπη, ης, ἡ — love; Rom 5:5.</p>" * 25,
    "B.2": "<p>Index</p>" + "".join(f"<p>{c}:{v}\t{c * 7 + v}</p>" for c in range(1, 9)
                                    for v in range(1, 12)),
}
ORDER = list(ARTICLES)


class FakeLogos:
    """The articles API: a book root, then a `nextArticleId` chain."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def _article(self, article_id: str) -> dict:
        index = ORDER.index(article_id)
        return {
            "resourceTitle": TITLE,
            "abbreviatedTitle": "TL",
            "article": {"articleId": article_id, "content": ARTICLES[article_id]},
            "nextArticleId": ORDER[index + 1] if index + 1 < len(ORDER) else None,
            "previousArticleId": ORDER[index - 1] if index else None,
        }

    async def get(self, path: str) -> dict:
        if path.endswith("/tableofcontents"):
            raise RuntimeError("this book has no table of contents")
        if "/articles/" in path:
            article_id = path.rsplit("/", 1)[1]
            self.fetched.append(article_id)
            return self._article(article_id)
        return self._article(ORDER[0])
