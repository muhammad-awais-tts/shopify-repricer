#!/usr/bin/env python3
"""
shopify-repricer — turn a supplier cost list into a Shopify price import.

A supplier sends you a cost list. Shopify gives you a product export. Between
the two sits an afternoon of spreadsheet work: match the SKUs, apply whatever
margin rule you use, work out the compare-at price, and paste the results back
into a file Shopify will accept.

This does that join, applies one pricing rule, and writes a CSV containing only
the columns Shopify needs to update a price. It also prints what would change
before you import anything, and refuses to write a row whose price moves more
than a threshold you set, because the usual cause of a price moving by 70% is a
bad line in the cost file rather than a real change at the supplier.

    Shopify admin -> Products -> Export -> All products -> Plain CSV

Usage:
    python3 shopify_repricer.py export.csv costs.csv -o updated.csv
    python3 shopify_repricer.py export.csv costs.csv            # dry run only
    python3 shopify_repricer.py export.csv costs.csv -o out.csv --round-99
    python3 shopify_repricer.py export.csv costs.csv -o out.csv --max-change 25

Exit code is 1 if any row broke the --max-change guard, so it can gate a job.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

MIN_MULTIPLE = 1.875      # the lowest retail price we will claim, as a multiple of cost
FALLBACK_MULTIPLE = 2.50  # used when the supplier gives no MSRP at all
DISCOUNT = 0.20           # how far below retail the selling price sits
MAX_CHANGE = 50.0         # percent a price may move before we refuse to write it

WIDTH = 74

# The only columns Shopify needs in order to update prices. Handle plus the
# option values identify the variant; everything else about the product is left
# alone because it is not in the file.
OUT_COLUMNS = [
    "Handle",
    "Title",
    "Option1 Name",
    "Option1 Value",
    "Variant SKU",
    "Variant Price",
    "Variant Compare At Price",
]

# Cost lists arrive with whatever header the supplier's system felt like. Names
# are compared after lowercasing, swapping _ and - for spaces, and collapsing
# runs of whitespace, so "Cost_Price" and "cost price" are the same column.
SKU_HEADERS = ("sku", "variant sku", "item sku", "supplier sku", "product sku")
COST_HEADERS = ("cost", "cost price", "cost per item", "unit cost",
                "wholesale", "wholesale price")
MSRP_HEADERS = ("msrp", "retail", "retail price", "list price", "rrp")


# --------------------------------------------------------------------------- helpers

def normalise_header(name: Optional[str]) -> str:
    raw = (name or "").replace("﻿", "").replace("_", " ").replace("-", " ")
    return " ".join(raw.lower().split())


def clean_sku(value: Optional[str]) -> str:
    """Shopify's export prefixes numeric SKUs with an apostrophe so spreadsheets behave."""
    return (value or "").strip().lstrip("'").strip()


def money(value: Optional[str]) -> Optional[float]:
    """A price or a cost, or None if the cell is empty or not a number.

    Supplier files are not Shopify files: they contain currency symbols and
    thousands separators, and those are not a reason to drop the row.
    """
    raw = (value or "").strip().lstrip("'").strip()
    for junk in ("$", "£", "€", ",", " "):
        raw = raw.replace(junk, "")
    if not raw:
        return None
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def round2(value: float) -> float:
    return round(value + 1e-9, 2)


def round_down_to_99(value: float) -> float:
    """Drop a price to the nearest .99 at or below it. 41.60 becomes 40.99.

    Prices under 0.99 are left alone; there is no .99 below them.
    """
    value = round2(value)
    if value < 0.99:
        return value
    candidate = math.floor(value) + 0.99
    if candidate > value + 1e-9:
        candidate -= 1.0
    return round2(candidate)


def fmt(value: Optional[float]) -> str:
    return "" if value is None else "{0:.2f}".format(value)


def plural(count: int, word: str, plural_word: Optional[str] = None) -> str:
    return "{0} {1}".format(count, word if count == 1 else (plural_word or word + "s"))


# --------------------------------------------------------------------------- model

class Variant:
    """One priceable row of a Shopify export, with the product fields filled in."""

    __slots__ = ("handle", "title", "option_name", "option_value",
                 "sku", "price", "compare_at")

    def __init__(self, handle, title, option_name, option_value, sku, price, compare_at):
        self.handle = handle
        self.title = title
        self.option_name = option_name
        self.option_value = option_value
        self.sku = sku
        self.price = price
        self.compare_at = compare_at

    def label(self) -> str:
        return self.option_value or self.sku or self.handle


class Cost:
    __slots__ = ("sku", "cost", "msrp")

    def __init__(self, sku: str, cost: float, msrp: Optional[float]):
        self.sku = sku
        self.cost = cost
        self.msrp = msrp


class Change:
    """What one variant's price would become, and how far that is from today."""

    __slots__ = ("variant", "cost", "retail", "price", "basis")

    def __init__(self, variant: Variant, cost: Cost, retail: float, price: float, basis: str):
        self.variant = variant
        self.cost = cost
        self.retail = retail
        self.price = price
        self.basis = basis

    @property
    def old_price(self) -> Optional[float]:
        return self.variant.price

    @property
    def delta(self) -> Optional[float]:
        if self.old_price is None:
            return None
        return round2(self.price - self.old_price)

    @property
    def pct(self) -> Optional[float]:
        """Percentage move. None when there is no previous price to measure against."""
        if self.old_price is None or self.old_price == 0:
            return None
        return (self.price - self.old_price) / self.old_price * 100.0

    @property
    def new_compare_at(self) -> Optional[float]:
        """Retail, unless that is not above the price — then no compare-at at all.

        A compare-at equal to or below the selling price shows the shopper a
        discount that is not one, so the column is left empty instead.
        """
        if self.retail <= self.price:
            return None
        return self.retail

    def as_row(self) -> dict:
        v = self.variant
        return {
            "Handle": v.handle,
            "Title": v.title,
            "Option1 Name": v.option_name,
            "Option1 Value": v.option_value,
            "Variant SKU": v.sku,
            "Variant Price": fmt(self.price),
            "Variant Compare At Price": fmt(self.new_compare_at),
        }


class Rule:
    __slots__ = ("min_multiple", "fallback_multiple", "discount", "round_99")

    def __init__(self, min_multiple=MIN_MULTIPLE, fallback_multiple=FALLBACK_MULTIPLE,
                 discount=DISCOUNT, round_99=False):
        self.min_multiple = min_multiple
        self.fallback_multiple = fallback_multiple
        self.discount = discount
        self.round_99 = round_99

    def apply(self, cost: Cost) -> Tuple[float, float, str]:
        """Return (retail, price, which branch of the rule decided the retail)."""
        floor = cost.cost * self.min_multiple
        if cost.msrp:
            if cost.msrp >= floor - 1e-9:
                retail, basis = cost.msrp, "msrp"
            else:
                retail, basis = floor, "msrp below the minimum multiple"
        else:
            retail, basis = cost.cost * self.fallback_multiple, "no msrp"
        retail = round2(retail)
        price = round2(retail * (1.0 - self.discount))
        if self.round_99:
            price = round_down_to_99(price)
        return retail, price, basis


class Plan:
    """Everything the run worked out, so the report and the writer agree."""

    def __init__(self):
        self.changes: List[Change] = []      # matched and within the guard
        self.violations: List[Change] = []   # matched but moving too far
        self.no_cost: List[Variant] = []     # a SKU with no line in the cost file
        self.no_sku: List[Variant] = []      # nothing to join on
        self.unused_costs: List[str] = []    # cost lines with no variant
        self.duplicate_costs: List[str] = [] # the same SKU twice in the cost file
        self.variant_count = 0
        self.product_count = 0

    @property
    def matched(self) -> int:
        return len(self.changes) + len(self.violations)

    @property
    def rises(self) -> List[Change]:
        return [c for c in self.changes if c.delta is None or c.delta > 0]

    @property
    def falls(self) -> List[Change]:
        return [c for c in self.changes if c.delta is not None and c.delta < 0]

    @property
    def unchanged(self) -> List[Change]:
        return [c for c in self.changes if c.delta == 0]

    @property
    def value_change(self) -> float:
        """Sum of the price moves across the rows that would be written."""
        return round2(sum(c.delta for c in self.changes if c.delta is not None))

    def movers(self, count: int = 5) -> List[Change]:
        moved = [c for c in self.changes if c.delta not in (None, 0)]
        return sorted(moved, key=lambda c: abs(c.delta), reverse=True)[:count]


# --------------------------------------------------------------------------- loading

def load_export(path: str) -> Tuple[List[Variant], int]:
    """Read a Shopify product export into priceable variant rows.

    One product spans several rows. Only the first row of a handle carries the
    product-level fields, so Title and Option1 Name are carried forward from the
    first row of the group that actually has them.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "Handle" not in reader.fieldnames:
            raise SystemExit(
                "{0} does not look like a Shopify product export (no 'Handle' "
                "column). Export with: Products -> Export -> Plain CSV.".format(path)
            )
        grouped = OrderedDict()  # handle -> list of raw rows
        for row in reader:
            key = (row.get("Handle") or "").strip()
            if key:
                grouped.setdefault(key, []).append(row)

    variants: List[Variant] = []
    for handle, rows in grouped.items():
        def first(column: str) -> str:
            for row in rows:
                value = (row.get(column) or "").strip()
                if value:
                    return value
            return ""

        title = first("Title")
        option_name = first("Option1 Name")
        for row in rows:
            price = money(row.get("Variant Price"))
            option_value = (row.get("Option1 Value") or "").strip()
            sku = clean_sku(row.get("Variant SKU"))
            # Image-only and option-only rows carry no price and no SKU; they
            # are part of the product, not a variant we can reprice.
            if price is None and not sku and not option_value:
                continue
            compare_at = money(row.get("Variant Compare At Price"))
            if compare_at == 0:
                compare_at = None  # Shopify exports "no sale price" as 0
            variants.append(Variant(handle, title, option_name, option_value,
                                    sku, price, compare_at))
    return variants, len(grouped)


def pick_column(fieldnames, candidates) -> Optional[str]:
    """The first column in the file whose normalised name is one we recognise."""
    for name in fieldnames or []:
        if normalise_header(name) in candidates:
            return name
    return None


def load_costs(path: str) -> Tuple["OrderedDict[str, Cost]", List[str]]:
    """Read a supplier cost list. Returns the costs by SKU and any duplicate SKUs."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        sku_col = pick_column(fields, SKU_HEADERS)
        cost_col = pick_column(fields, COST_HEADERS)
        if not sku_col or not cost_col:
            missing = []
            if not sku_col:
                missing.append("a SKU column (" + ", ".join(SKU_HEADERS) + ")")
            if not cost_col:
                missing.append("a cost column (" + ", ".join(COST_HEADERS) + ")")
            raise SystemExit(
                "{0} is missing {1}.\nIt has: {2}".format(
                    path, " and ".join(missing), ", ".join(fields) or "no header row")
            )
        msrp_col = pick_column(fields, MSRP_HEADERS)

        costs: "OrderedDict[str, Cost]" = OrderedDict()
        duplicates: List[str] = []
        for row in reader:
            sku = clean_sku(row.get(sku_col))
            cost = money(row.get(cost_col))
            if not sku or cost is None:
                continue
            if sku in costs:
                duplicates.append(sku)  # first line wins; a later one is ignored
                continue
            msrp = money(row.get(msrp_col)) if msrp_col else None
            costs[sku] = Cost(sku, cost, msrp)
    return costs, duplicates


# --------------------------------------------------------------------------- planning

def build_plan(variants, product_count, costs, duplicates, rule: Rule,
               max_change: float) -> Plan:
    plan = Plan()
    plan.variant_count = len(variants)
    plan.product_count = product_count
    plan.duplicate_costs = duplicates

    seen = set()
    for variant in variants:
        if not variant.sku:
            plan.no_sku.append(variant)
            continue
        cost = costs.get(variant.sku)
        if cost is None:
            plan.no_cost.append(variant)
            continue
        seen.add(variant.sku)
        retail, price, basis = rule.apply(cost)
        change = Change(variant, cost, retail, price, basis)
        pct = change.pct
        if pct is not None and abs(pct) > max_change + 1e-9:
            plan.violations.append(change)
        else:
            plan.changes.append(change)

    plan.unused_costs = [sku for sku in costs if sku not in seen]
    return plan


# --------------------------------------------------------------------------- output

def line(change: Change) -> str:
    old = fmt(change.old_price) or "-"
    pct = "   new" if change.pct is None else "{0:+.1f}%".format(change.pct)
    delta = "" if change.delta is None else "{0:+.2f}".format(change.delta)
    return "    {0:<24.24} {1:<8.8} {2:>8} -> {3:>8}  {4:>8} {5:>7}".format(
        change.variant.handle, change.variant.label(), old,
        fmt(change.price), delta, pct)


def report(plan: Plan, rule: Rule, max_change: float, out_path: Optional[str]) -> None:
    bar = "-" * WIDTH
    print()
    print("=" * WIDTH)
    print("  Shopify repricer" + ("" if out_path else " — dry run"))
    print("=" * WIDTH)
    print("  rule   retail = msrp, but never below cost x {0:g}".format(rule.min_multiple))
    print("         no msrp: retail = cost x {0:g}".format(rule.fallback_multiple))
    print("         price  = retail less {0:g}%{1}".format(
        rule.discount * 100, ", rounded down to .99" if rule.round_99 else ""))
    print("  guard  refuse any price moving more than {0:g}%".format(max_change))
    print(bar)
    print("  {0:>6}  variants in the export ({1} products)".format(
        plan.variant_count, plan.product_count))
    print("  {0:>6}  matched to a cost line".format(plan.matched))
    print("  {0:>6}  in the export with no cost line for that SKU".format(len(plan.no_cost)))
    print("  {0:>6}  in the export with no SKU, nothing to join on".format(len(plan.no_sku)))
    print("  {0:>6}  cost lines with no matching variant".format(len(plan.unused_costs)))
    if plan.duplicate_costs:
        print("  {0:>6}  repeated SKUs in the cost list, the first line used".format(
            len(plan.duplicate_costs)))
    print(bar)
    print("  {0:>6}  prices rise".format(len(plan.rises)))
    print("  {0:>6}  prices fall".format(len(plan.falls)))
    print("  {0:>6}  unchanged".format(len(plan.unchanged)))
    print("  {0:>+6.2f}  change in catalogue value across the {1} to write".format(
        plan.value_change, plural(len(plan.changes), "row")))

    movers = plan.movers()
    if movers:
        print(bar)
        print("  Biggest movers")
        print("    {0:<24} {1:<8} {2:>8}    {3:>8}  {4:>8} {5:>7}".format(
            "handle", "variant", "now", "new", "delta", "change"))
        for change in movers:
            print(line(change))

    leftovers = ((plan.no_cost, "In the export, no cost line"),
                 (plan.unused_costs, "In the cost list, no variant"))
    if any(items for items, _ in leftovers):
        print(bar)
        for items, label in leftovers:
            if not items:
                continue
            shown = [i if isinstance(i, str) else (i.sku or i.handle) for i in items[:6]]
            print("  {0:<28} {1}{2}".format(
                label, ", ".join(shown), " …" if len(items) > 6 else ""))

    print(bar)
    if plan.violations:
        print("  ! {0} {1} more than {2:g}% — not written".format(
            plural(len(plan.violations), "row"),
            "moves" if len(plan.violations) == 1 else "move", max_change))
        print("    A wrong cost line is the usual cause. Check it before importing.")
        for change in sorted(plan.violations,
                             key=lambda c: abs(c.pct or 0), reverse=True):
            print(line(change))
    else:
        print("  No row moved more than {0:g}%.".format(max_change))
    print(bar)
    if out_path:
        print("  {0} written to {1}".format(plural(len(plan.changes), "row"), out_path))
        print("  Open it and read it before importing it at Products -> Import.")
    else:
        print("  Nothing written. Pass -o updated.csv to produce the import file.")
    print("=" * WIDTH)
    print()


def write_csv(path: str, plan: Plan) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for change in plan.changes:
            writer.writerow(change.as_row())


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shopify_repricer.py",
        description="Join a supplier cost list to a Shopify export and write a price import.",
        epilog="Export from: Shopify admin -> Products -> Export -> All products -> Plain CSV",
    )
    parser.add_argument("export", help="path to a Shopify product CSV export")
    parser.add_argument("costs", help="path to a supplier cost CSV (SKU, cost, optional MSRP)")
    parser.add_argument("-o", "--out", default=None,
                        help="write the Shopify import CSV here; omit for a dry run")
    parser.add_argument("--min-multiple", type=float, default=MIN_MULTIPLE,
                        metavar="N",
                        help="lowest retail price as a multiple of cost "
                             "(default {0:g})".format(MIN_MULTIPLE))
    parser.add_argument("--fallback-multiple", type=float, default=FALLBACK_MULTIPLE,
                        metavar="N",
                        help="retail multiple used when there is no MSRP "
                             "(default {0:g})".format(FALLBACK_MULTIPLE))
    parser.add_argument("--discount", type=float, default=DISCOUNT, metavar="FRACTION",
                        help="how far below retail the price sits, 0 to 1 "
                             "(default {0:g})".format(DISCOUNT))
    parser.add_argument("--round-99", action="store_true",
                        help="drop each price to the nearest .99 at or below it")
    parser.add_argument("--max-change", type=float, default=MAX_CHANGE, metavar="PCT",
                        help="refuse any price moving more than this percent "
                             "(default {0:g})".format(MAX_CHANGE))
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.min_multiple <= 0 or args.fallback_multiple <= 0:
        parser.error("--min-multiple and --fallback-multiple must be greater than 0")
    if not 0 <= args.discount < 1:
        parser.error("--discount is a fraction between 0 and 1: use 0.20, not 20")
    if args.max_change < 0:
        parser.error("--max-change is a percentage and cannot be negative")

    variants, product_count = load_export(args.export)
    if not variants:
        raise SystemExit("no priceable variants found in {0}".format(args.export))

    costs, duplicates = load_costs(args.costs)
    if not costs:
        raise SystemExit("no usable cost lines found in {0}".format(args.costs))

    rule = Rule(args.min_multiple, args.fallback_multiple, args.discount, args.round_99)
    plan = build_plan(variants, product_count, costs, duplicates, rule, args.max_change)

    if args.out and plan.changes:
        write_csv(args.out, plan)
    report(plan, rule, args.max_change, args.out if plan.changes else None)

    return 1 if plan.violations else 0


if __name__ == "__main__":
    sys.exit(main())
