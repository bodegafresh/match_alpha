from app.models.poisson_model import poisson_1x2


def test_poisson_1x2_sums_to_one() -> None:
    probs = poisson_1x2(1.4, 1.1)
    assert set(probs) == {"HOME", "DRAW", "AWAY"}
    assert abs(sum(probs.values()) - 1.0) < 1e-9

