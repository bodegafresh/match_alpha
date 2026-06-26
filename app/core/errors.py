class AppError(Exception):
    """Base application error."""


class DataQualityError(AppError):
    """Raised when canonical data cannot be safely produced."""


class BettingBlocked(AppError):
    """Raised when a decision cannot become bettable."""

