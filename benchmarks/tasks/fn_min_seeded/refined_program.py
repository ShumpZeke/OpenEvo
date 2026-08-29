# EVOLVE-BLOCK-START
"""Random search plus a local refinement pass.

Genuinely better than the seed, and by a margin small enough to be interesting:
the point of scoring it is to check that the seeded evaluator can see an
improvement that the unseeded one buries in its own 0.39 of noise.
"""
import numpy as np


def search_algorithm(iterations=1000, bounds=(-5, 5)):
    best_x = np.random.uniform(bounds[0], bounds[1])
    best_y = np.random.uniform(bounds[0], bounds[1])
    best_value = evaluate_function(best_x, best_y)

    # Spend 70% of the budget exploring, as the seed does.
    explore = int(iterations * 0.7)
    for _ in range(explore):
        x = np.random.uniform(bounds[0], bounds[1])
        y = np.random.uniform(bounds[0], bounds[1])
        value = evaluate_function(x, y)
        if value < best_value:
            best_value = value
            best_x, best_y = x, y

    # Spend the rest refining around the best point found, shrinking the radius.
    step = (bounds[1] - bounds[0]) * 0.1
    for i in range(iterations - explore):
        x = np.clip(best_x + np.random.normal(0, step), bounds[0], bounds[1])
        y = np.clip(best_y + np.random.normal(0, step), bounds[0], bounds[1])
        value = evaluate_function(x, y)
        if value < best_value:
            best_value = value
            best_x, best_y = x, y
        if i % 50 == 49:
            step *= 0.8

    return best_x, best_y, best_value


# EVOLVE-BLOCK-END


# This part remains fixed (not evolved)
def evaluate_function(x, y):
    """The complex function we're trying to minimize"""
    return np.sin(x) * np.cos(y) + np.sin(x * y) + (x**2 + y**2) / 20


def run_search():
    x, y, value = search_algorithm()
    return x, y, value


if __name__ == "__main__":
    x, y, value = run_search()
    print(f"Found minimum at ({x}, {y}) with value {value}")
