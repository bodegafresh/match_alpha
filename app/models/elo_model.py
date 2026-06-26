from math import pow


def elo_expected(rating_a: float, rating_b: float) -> float:
    return 1 / (1 + pow(10, (rating_b - rating_a) / 400))


def elo_update(rating: float, opponent_rating: float, score: float, k_factor: float = 20) -> float:
    return rating + k_factor * (score - elo_expected(rating, opponent_rating))

