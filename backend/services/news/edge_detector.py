"""Compatibility import for the canonical news edge estimator.

The strategy and AI tool historically imported ``edge_detector``.  Estimation,
filtering, timeout handling, and caching now live only in ``edge_estimator``.
"""

from services.news.edge_estimator import EdgeEstimator, NewsEdge, edge_estimator


NewsEdgeDetector = EdgeEstimator
edge_detector = edge_estimator

__all__ = ["NewsEdge", "NewsEdgeDetector", "edge_detector"]
