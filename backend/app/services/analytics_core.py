"""The analysis maths, kept free of the database so it is easy to test and to reason about.

Every figure on the Analytics page comes from :func:`compute_stats`, which is fed one
:class:`Row` per violation. A row is a live event (reviewed or still waiting) or an archived /
imported one (always reviewed). See app.services.analytics for how the rows are loaded.

Definitions (they follow the "Mersey events" spreadsheet the team already uses):

* total violations   every event the site has ever held: waiting + reviewed + archived/imported
* reviewed events    TP + FP + Unsure
* precision          TP / (TP + FP)           (Unsure is left out, as in the sheet)
* precision excl.    TP / (TP + FP that were NOT marked unclear)
* unclear images     reviewed events whose image was marked unclear; clear = reviewed - unclear
* MMC                make / model / colour accuracy over the "MMC reviewed" events, i.e. reviewed
                     events where at least one of the three ticks/crosses was filled in. Each rate
                     is (events with that tick) / (MMC reviewed events).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Row:
    label: str | None            # 'TP' | 'FP' | 'Unsure' | None (not reviewed yet)
    clear: bool | None = None    # image_clarity_clear: True clear, False unclear, None not rated
    make: bool | None = None     # make_correct
    model: bool | None = None    # model_correct
    color: bool | None = None    # color_correct
    archived: bool = False       # removed from the site after a download, or imported from xlsx


def _ratio(numerator: int, denominator: int) -> float | None:
    """A 0-1 fraction, or ``None`` when there is nothing to divide by (the page shows a dash)."""
    return numerator / denominator if denominator else None


def compute_stats(rows: Iterable[Row]) -> dict[str, int | float | None]:
    total = pending = archived = to_review = 0
    tp = fp = unsure = 0
    unclear = fp_unclear = 0
    mmc = make_ok = model_ok = color_ok = 0

    for row in rows:
        total += 1
        if row.archived:
            archived += 1
        if row.label is None:
            pending += 1
            if not row.archived:
                to_review += 1
            continue
        if row.label == "TP":
            tp += 1
        elif row.label == "FP":
            fp += 1
            if row.clear is False:
                fp_unclear += 1
        elif row.label.lower() == "unsure":
            unsure += 1
        else:  # an unknown label is not a verdict; do not let it distort the totals
            total -= 1
            if row.archived:
                archived -= 1
            continue
        if row.clear is False:
            unclear += 1
        if row.make is not None or row.model is not None or row.color is not None:
            mmc += 1
            make_ok += row.make is True
            model_ok += row.model is True
            color_ok += row.color is True

    reviewed = tp + fp + unsure
    return {
        "total_violations": total,
        "pending": pending,
        "to_review": to_review,        # pending events that are really on the site (can be reviewed)
        "archived": archived,          # of the total: removed after download or imported from xlsx
        "reviewed": reviewed,
        "tp": tp,
        "fp": fp,
        "unsure": unsure,
        "clear_images": reviewed - unclear,
        "unclear_images": unclear,
        "fp_clear": fp - fp_unclear,
        "fp_unclear": fp_unclear,
        "precision": _ratio(tp, tp + fp),
        "precision_excl_unclear_fp": _ratio(tp, tp + fp - fp_unclear),
        "mmc_reviewed": mmc,
        "make_correct": make_ok,
        "model_correct": model_ok,
        "color_correct": color_ok,
        "make_accuracy": _ratio(make_ok, mmc),
        "model_accuracy": _ratio(model_ok, mmc),
        "color_accuracy": _ratio(color_ok, mmc),
    }
