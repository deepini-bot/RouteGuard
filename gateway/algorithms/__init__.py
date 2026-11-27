from gateway.algorithms.sliding_window import SlidingWindow
from gateway.algorithms.token_bucket import RateResult, TokenBucket

ALGORITHMS = {
    TokenBucket.name: TokenBucket,
    SlidingWindow.name: SlidingWindow,
}

__all__ = ["ALGORITHMS", "RateResult", "TokenBucket", "SlidingWindow"]
