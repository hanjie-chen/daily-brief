"""Search-based recovery of article material after the original source is blocked."""

from .alternate_reporting import (
    AlternateReportingCandidate,
    AlternateReportingFinder,
    AlternateReportingFinderError,
)
from .fetched_material import FetchedMaterial, coerce_fetched_material
from .same_article import SameArticleCandidate, SameArticleFinder, SameArticleFinderError
from .search_recovery import (
    RecoveryOutcome,
    attempt_alternate_reporting_recovery,
    attempt_same_article_recovery,
    attempt_syndicated_recovery,
)
from .syndicated_copy import (
    SyndicatedCandidate,
    SyndicatedCopyFinder,
    SyndicatedFinderError,
    is_reuters_url,
)


__all__ = [
    "AlternateReportingCandidate",
    "AlternateReportingFinder",
    "AlternateReportingFinderError",
    "FetchedMaterial",
    "RecoveryOutcome",
    "SameArticleCandidate",
    "SameArticleFinder",
    "SameArticleFinderError",
    "SyndicatedCandidate",
    "SyndicatedCopyFinder",
    "SyndicatedFinderError",
    "attempt_alternate_reporting_recovery",
    "attempt_same_article_recovery",
    "attempt_syndicated_recovery",
    "coerce_fetched_material",
    "is_reuters_url",
]
