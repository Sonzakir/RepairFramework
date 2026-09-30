from apr_framework.core.exceptions import ConfigurationError
from apr_framework.repair.ranking.base import PatchRanker
from apr_framework.repair.ranking.weighted import WeightedCompositeRanker


def create_ranker(ranker_name: str, **kwargs) -> PatchRanker:
    """Instantiate a PatchRanker by name.

    Args:
        ranker_name: Registered ranker identifier (currently ``"weighted"``).
        **kwargs:    Constructor arguments forwarded to the ranker class.

    Raises:
        ConfigurationError: If ``ranker_name`` is not a known ranker.
    """
    if ranker_name == "weighted":
        return WeightedCompositeRanker(**kwargs)
    raise ConfigurationError(
        f"Unknown patch ranker {ranker_name!r}. Valid choices: weighted"
    )
