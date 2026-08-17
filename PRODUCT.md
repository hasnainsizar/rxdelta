# Product

## Register

product

## Platform

web

## Users

Health plan analysts, pharmacy benefits managers, and health policy researchers.
Technical and detail-oriented. They open the report already knowing the domain,
scan it for the handful of rows that affect the plans they are responsible for,
and often read it as a printed or PDF copy in grayscale. The job to be done is
narrow: find what changed this month that will cost a member money, decide
whether it needs escalating, and be able to defend the number to someone else.

## Product Purpose

rxdelta compares two monthly CMS Medicare Part D snapshots and reports the
formulary changes that affect what a plan member pays out of pocket. The HTML
report is the deliverable: a single self contained file that states what was
compared, ranks the changes by estimated member impact, and is explicit about
what the estimate does not cover. Success is a reader trusting the numbers
enough to act on them, and understanding the limits well enough not to overstate
them.

## Positioning

The only formulary diff that puts the estimate's uncertainty on the same page as
the estimate, in the same weight of type.

## Brand Personality

Restrained, clinical, precise. The voice is that of a regulatory filing or a
clinical summary: it states findings, qualifies them, and stops. It never sells,
never celebrates a number, and never uses emphasis it has not earned. Trust comes
from being visibly careful, not from looking confident.

## Anti-references

SaaS marketing pages. Admin dashboards. Purple or blue gradients. Card in card
nesting. Icon tiles above headings. Inter. Anything that reads as an interface
rather than a document: hero metrics, KPI tiles, status pills used decoratively,
color as ornament.

## Design Principles

Say what is uncertain as loudly as what is known. The limitations block is not a
footnote and is never collapsed; it carries the same visual weight as the
findings it qualifies.

Print is a first class target, not a fallback. A reader with a grayscale US
Letter printout must lose no information, which means color never carries meaning
on its own.

Density is a feature. This audience reads tables. Give them a table that rewards
scanning rather than padding that pushes rows off the screen.

Numbers are the interface. Cost figures, tier levels and severity scores align on
their digits, and a range is always visibly a range, never a point estimate
wearing a range's clothes.

Nothing decorative. Every rule, weight, and shade is doing a job a reader could
name. If it cannot be justified, it is removed.

## Accessibility & Inclusion

WCAG AA on every text and background pair, including severity color coding.
Severity and direction are always paired with a label or symbol so meaning
survives grayscale printing and color vision deficiency. Readable at 375px
width. Motion is minimal by nature here and must respect
`prefers-reduced-motion`.
