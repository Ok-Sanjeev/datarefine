import pytest
from rate_limiter import limiter

@pytest.fixture(autouse=True)
def reset_rate_limiter_globally():
    """Reset rate limiter state before and after every test to guarantee clean isolation across the entire suite."""
    limiter.reset()
    yield
    limiter.reset()
