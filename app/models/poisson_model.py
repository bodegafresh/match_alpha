from math import exp, factorial


def poisson_pmf(k: int, lam: float) -> float:
    if lam < 0:
        raise ValueError("lambda must be non-negative")
    return exp(-lam) * (lam ** k) / factorial(k)


def poisson_1x2(home_lambda: float, away_lambda: float, max_goals: int = 10) -> dict[str, float]:
    if home_lambda <= 0 or away_lambda <= 0:
        raise ValueError("lambdas must be positive")
    home = draw = away = 0.0
    for hg in range(max_goals + 1):
        hp = poisson_pmf(hg, home_lambda)
        for ag in range(max_goals + 1):
            p = hp * poisson_pmf(ag, away_lambda)
            if hg > ag:
                home += p
            elif hg == ag:
                draw += p
            else:
                away += p
    total = home + draw + away
    return {
        "HOME": home / total,
        "DRAW": draw / total,
        "AWAY": away / total,
    }

