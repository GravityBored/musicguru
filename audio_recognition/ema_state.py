class EMAState:
    """Exponential moving average over recognition hits.

    A key must accumulate `threshold` weight before it is accepted as the
    current track, which suppresses one-off misrecognitions.
    """

    def __init__(self, alpha: float = 0.5, threshold: float = 0.7, prune_epsilon: float = 1e-3):
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be strictly between 0 and 1")
        self.alpha = alpha
        self.threshold = threshold
        self.prune_epsilon = prune_epsilon
        self.scores: dict[str, float] = {}
        self.current_key: str | None = None

    def update(self, key: str) -> bool:
        """Decay all scores, boost `key`, return True if the current track changed."""
        decayed = {}
        for k, v in self.scores.items():
            v *= self.alpha
            # Was a plain defaultdict that kept every key ever seen forever and
            # multiplied all of them on every update.
            if v >= self.prune_epsilon or k == key or k == self.current_key:
                decayed[k] = v
        self.scores = decayed

        self.scores[key] = self.scores.get(key, 0.0) + (1.0 - self.alpha)

        if key != self.current_key and self.scores[key] >= self.threshold:
            self.current_key = key
            return True
        return False

    def reset(self) -> None:
        """Forget the current track (e.g. after a long silence)."""
        self.scores.clear()
        self.current_key = None
