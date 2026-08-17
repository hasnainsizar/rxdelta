"""The single source of the limitations text.

Terminal output and the HTML report both render this list, so the two cannot
drift apart. Any new caveat goes here and shows up everywhere.
"""

from __future__ import annotations

LIMITATIONS_TITLE = "What these estimates do not account for"

LIMITATIONS: tuple[str, ...] = (
    "The deductible phase. A member who has not met the deductible usually pays the full "
    "negotiated price, not the cost sharing shown here.",
    "The catastrophic phase. Only the initial coverage phase is priced. The Part D benefit "
    "has had three phases since 2025, deductible, initial coverage and catastrophic; the "
    "coverage gap phase no longer exists.",
    "Low income subsidy status. A member with extra help pays subsidy amounts set by CMS, "
    "not the plan's published cost sharing.",
    "The quantity actually dispensed. Amounts are scaled to a 30 day supply, which is not "
    "the same as what a given prescription fills.",
    "Negotiated rebates and any pharmacy specific pricing that is not published in these files.",
    "Manufacturer discounts. CMS states that these files do not reflect discounts applied "
    "under the Medicare Part D Manufacturer Discount Program.",
    "Whether anyone is taking the drug. A tier move only costs a member money if that member "
    "fills that drug.",
)

ESTIMATE_NOTE = (
    "Cost figures are ranges, not predictions. The range spans preferred, non preferred and "
    "mail order pharmacies and every supply length the plan publishes, scaled to 30 days."
)

OPEN_ENDED_NOTE = (
    "A range marked 'or more' covers a drug joining or leaving the formulary. The price "
    "without formulary coverage is not in the CMS files, so only the covered side is priced."
)
