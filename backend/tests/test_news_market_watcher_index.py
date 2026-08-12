import sys
import threading
from pathlib import Path

import numpy as np

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.news.market_watcher_index import IndexedMarket, MarketWatcherIndex


def test_keyword_index_is_available_while_embedding_rebuild_is_running():
    encode_started = threading.Event()
    release_encode = threading.Event()

    class BlockingModel:
        def encode(self, texts, **kwargs):
            encode_started.set()
            release_encode.wait(timeout=2.0)
            return np.ones((len(texts), 2), dtype=np.float32)

    index = MarketWatcherIndex()
    index._initialized = True
    index._model = BlockingModel()
    market = IndexedMarket(
        market_id="market-1",
        question="Will the Federal Reserve cut interest rates?",
        event_title="Federal Reserve decision",
        category="economy",
        yes_price=0.4,
        no_price=0.6,
        liquidity=10_000.0,
    )

    index.rebuild_keywords([market])
    rebuild_thread = threading.Thread(target=index.rebuild, args=([market],))
    rebuild_thread.start()
    assert encode_started.wait(timeout=1.0)

    try:
        results = index.search(
            query_terms=["federal", "reserve"],
            top_k=5,
            keyword_weight=1.0,
            semantic_weight=0.0,
        )
    finally:
        release_encode.set()
        rebuild_thread.join(timeout=2.0)

    assert [result.market.market_id for result in results] == ["market-1"]
