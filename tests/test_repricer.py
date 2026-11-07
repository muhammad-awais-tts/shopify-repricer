"""Every rule gets a case that trips it and a near miss that does not.

CSVs are written to real temporary files and read back through the real
loaders, so the export quirks and the cost-header matching are exercised
rather than mocked.
"""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shopify_repricer as sr

EXPORT_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Published",
    "Option1 Name", "Option1 Value", "Variant SKU", "Variant Inventory Qty",
    "Variant Price", "Variant Compare At Price", "Image Src",
    "Cost per item", "Status",
]

VARIANT = {
    "Handle": "demo-product",
    "Title": "Demo Product",
    "Body (HTML)": "<p>Demo.</p>",
    "Vendor": "Demo Vendor",
    "Published": "TRUE",
    "Option1 Name": "Size",
    "Option1 Value": "One Size",
    "Variant SKU": "500001",
    "Variant Inventory Qty": "10",
    "Variant Price": "40.00",
    "Variant Compare At Price": "0",
    "Image Src": "https://example.com/a.jpg",
    "Status": "active",
}


def variant_row(**overrides):
    out = dict(VARIANT)
    out.update(overrides)
    return out


def write_csv(columns, rows):
    """Write rows to a temp CSV and return the path. Caller deletes it."""
    handle, path = tempfile.mkstemp(suffix=".csv")
    os.close(handle)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})
    return path


def load_export(rows):
    path = write_csv(EXPORT_COLUMNS, rows)
    try:
        return sr.load_export(path)
    finally:
        os.unlink(path)


def load_costs(columns, rows):
    path = write_csv(columns, rows)
    try:
        return sr.load_costs(path)
    finally:
        os.unlink(path)


def plan_for(export_rows, cost_rows, cost_columns=("sku", "cost", "msrp"),
             rule=None, max_change=sr.MAX_CHANGE):
    variants, products = load_export(export_rows)
    costs, duplicates = load_costs(list(cost_columns), cost_rows)
    return sr.build_plan(variants, products, costs, duplicates,
                         rule or sr.Rule(), max_change)


# --------------------------------------------------------------------------- rule

class PricingRule(unittest.TestCase):
    def setUp(self):
        self.rule = sr.Rule()

    def test_msrp_above_the_floor_becomes_retail(self):
        retail, price, basis = self.rule.apply(sr.Cost("s", 20.00, 52.00))
        self.assertEqual((retail, price), (52.00, 41.60))
        self.assertEqual(basis, "msrp")

    def test_msrp_exactly_at_the_floor_is_still_used(self):
        """20 x 1.875 is 37.50; an MSRP of exactly 37.50 is not 'too low'."""
        retail, price, basis = self.rule.apply(sr.Cost("s", 20.00, 37.50))
        self.assertEqual((retail, price), (37.50, 30.00))
        self.assertEqual(basis, "msrp")

    def test_msrp_a_penny_below_the_floor_is_replaced(self):
        retail, _, basis = self.rule.apply(sr.Cost("s", 20.00, 37.49))
        self.assertEqual(retail, 37.50)
        self.assertIn("below", basis)

    def test_msrp_well_below_the_floor_is_replaced_by_the_multiple(self):
        retail, price, _ = self.rule.apply(sr.Cost("s", 14.00, 19.00))
        self.assertEqual((retail, price), (26.25, 21.00))

    def test_no_msrp_uses_the_fallback_multiple(self):
        retail, price, basis = self.rule.apply(sr.Cost("s", 16.00, None))
        self.assertEqual((retail, price), (40.00, 32.00))
        self.assertEqual(basis, "no msrp")

    def test_msrp_of_zero_counts_as_no_msrp(self):
        retail, _, basis = self.rule.apply(sr.Cost("s", 16.00, 0.0))
        self.assertEqual(retail, 40.00)
        self.assertEqual(basis, "no msrp")

    def test_min_multiple_is_configurable(self):
        rule = sr.Rule(min_multiple=3.0)
        retail, _, _ = rule.apply(sr.Cost("s", 10.00, 20.00))
        self.assertEqual(retail, 30.00)

    def test_fallback_multiple_is_configurable(self):
        rule = sr.Rule(fallback_multiple=4.0)
        retail, _, _ = rule.apply(sr.Cost("s", 10.00, None))
        self.assertEqual(retail, 40.00)

    def test_discount_is_configurable(self):
        rule = sr.Rule(discount=0.35)
        _, price, _ = rule.apply(sr.Cost("s", 20.00, 100.00))
        self.assertEqual(price, 65.00)

    def test_zero_discount_leaves_price_at_retail(self):
        rule = sr.Rule(discount=0.0)
        retail, price, _ = rule.apply(sr.Cost("s", 20.00, 100.00))
        self.assertEqual(retail, price)

    def test_retail_and_price_are_rounded_to_two_places(self):
        retail, price, _ = self.rule.apply(sr.Cost("s", 7.77, None))
        self.assertEqual((retail, price), (19.43, 15.54))


# --------------------------------------------------------------------------- .99

class RoundTo99(unittest.TestCase):
    def test_a_price_drops_to_the_nearest_99_below_it(self):
        self.assertEqual(sr.round_down_to_99(41.60), 40.99)

    def test_a_price_already_on_99_is_left_alone(self):
        self.assertEqual(sr.round_down_to_99(40.99), 40.99)

    def test_a_whole_number_drops_a_penny(self):
        self.assertEqual(sr.round_down_to_99(40.00), 39.99)

    def test_a_price_below_99_cents_is_left_alone(self):
        """There is no .99 at or below 0.50, so nothing sensible to round to."""
        self.assertEqual(sr.round_down_to_99(0.50), 0.50)

    def test_the_rule_applies_it_to_the_price(self):
        rule = sr.Rule(round_99=True)
        _, price, _ = rule.apply(sr.Cost("s", 20.00, 52.00))
        self.assertEqual(price, 40.99)

    def test_the_rule_does_not_apply_it_to_the_compare_at(self):
        rule = sr.Rule(round_99=True)
        retail, _, _ = rule.apply(sr.Cost("s", 20.00, 52.00))
        self.assertEqual(retail, 52.00)


# --------------------------------------------------------------------------- export quirks

class ExportFormat(unittest.TestCase):
    def test_apostrophe_prefixed_sku_joins_to_a_plain_one(self):
        """Shopify writes numeric SKUs as '500001; the supplier writes 500001."""
        plan = plan_for([variant_row(**{"Variant SKU": "'500001"})],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(plan.matched, 1)
        self.assertEqual(plan.changes[0].variant.sku, "500001")

    def test_one_product_over_several_rows_becomes_several_variants(self):
        rows = [
            variant_row(**{"Option1 Value": "Small", "Variant SKU": "500001"}),
            variant_row(Title="", **{"Option1 Name": "", "Option1 Value": "Large",
                                     "Variant SKU": "500002"}),
        ]
        variants, products = load_export(rows)
        self.assertEqual((len(variants), products), (2, 1))

    def test_title_is_carried_to_rows_that_do_not_repeat_it(self):
        rows = [
            variant_row(),
            variant_row(Title="", **{"Option1 Value": "Large", "Variant SKU": "500002"}),
        ]
        variants, _ = load_export(rows)
        self.assertEqual([v.title for v in variants], ["Demo Product", "Demo Product"])

    def test_option_name_is_carried_to_rows_that_do_not_repeat_it(self):
        rows = [
            variant_row(),
            variant_row(**{"Option1 Name": "", "Option1 Value": "Large",
                           "Variant SKU": "500002"}),
        ]
        variants, _ = load_export(rows)
        self.assertEqual([v.option_name for v in variants], ["Size", "Size"])

    def test_an_image_only_row_is_not_a_variant(self):
        rows = [
            variant_row(),
            {"Handle": "demo-product", "Image Src": "https://example.com/b.jpg"},
        ]
        variants, products = load_export(rows)
        self.assertEqual((len(variants), products), (1, 1))

    def test_compare_at_of_zero_reads_as_no_compare_at(self):
        variants, _ = load_export([variant_row(**{"Variant Compare At Price": "0"})])
        self.assertIsNone(variants[0].compare_at)

    def test_a_real_compare_at_is_kept(self):
        variants, _ = load_export([variant_row(**{"Variant Compare At Price": "60.00"})])
        self.assertEqual(variants[0].compare_at, 60.00)

    def test_rows_with_no_handle_are_dropped(self):
        variants, products = load_export([variant_row(), variant_row(Handle="")])
        self.assertEqual((len(variants), products), (1, 1))

    def test_a_file_without_a_handle_column_is_rejected(self):
        path = write_csv(["sku", "cost"], [{"sku": "1", "cost": "2"}])
        try:
            with self.assertRaises(SystemExit) as caught:
                sr.load_export(path)
            self.assertIn("Handle", str(caught.exception))
        finally:
            os.unlink(path)


# --------------------------------------------------------------------------- cost headers

class CostFileHeaders(unittest.TestCase):
    def costs_from(self, columns, row):
        costs, _ = load_costs(columns, [row])
        return costs["500001"]

    def test_plain_lowercase_headers(self):
        cost = self.costs_from(["sku", "cost", "msrp"],
                               {"sku": "500001", "cost": "20.00", "msrp": "52.00"})
        self.assertEqual((cost.cost, cost.msrp), (20.00, 52.00))

    def test_shopify_style_headers(self):
        cost = self.costs_from(["Variant SKU", "Cost per item", "MSRP"],
                               {"Variant SKU": "500001", "Cost per item": "20.00",
                                "MSRP": "52.00"})
        self.assertEqual((cost.cost, cost.msrp), (20.00, 52.00))

    def test_underscored_headers(self):
        cost = self.costs_from(["sku", "cost_price", "list_price"],
                               {"sku": "500001", "cost_price": "20.00",
                                "list_price": "52.00"})
        self.assertEqual((cost.cost, cost.msrp), (20.00, 52.00))

    def test_headers_with_stray_case_and_whitespace(self):
        cost = self.costs_from([" SKU ", "  Cost  Price", "Retail Price"],
                               {" SKU ": "500001", "  Cost  Price": "20.00",
                                "Retail Price": "52.00"})
        self.assertEqual((cost.cost, cost.msrp), (20.00, 52.00))

    def test_msrp_column_is_optional(self):
        cost = self.costs_from(["sku", "cost"], {"sku": "500001", "cost": "20.00"})
        self.assertIsNone(cost.msrp)

    def test_a_missing_cost_column_is_an_error_that_names_the_aliases(self):
        path = write_csv(["sku", "price"], [{"sku": "500001", "price": "20.00"}])
        try:
            with self.assertRaises(SystemExit) as caught:
                sr.load_costs(path)
            self.assertIn("cost per item", str(caught.exception))
        finally:
            os.unlink(path)

    def test_a_missing_sku_column_is_an_error(self):
        path = write_csv(["item", "cost"], [{"item": "500001", "cost": "20.00"}])
        try:
            with self.assertRaises(SystemExit):
                sr.load_costs(path)
        finally:
            os.unlink(path)

    def test_currency_symbols_and_separators_are_parsed(self):
        cost = self.costs_from(["sku", "cost", "msrp"],
                               {"sku": "500001", "cost": "$1,250.00", "msrp": "2,999.00"})
        self.assertEqual((cost.cost, cost.msrp), (1250.00, 2999.00))

    def test_a_cost_that_is_not_a_number_skips_the_line(self):
        costs, _ = load_costs(["sku", "cost"],
                              [{"sku": "500001", "cost": "ask us"},
                               {"sku": "500002", "cost": "9.00"}])
        self.assertEqual(list(costs), ["500002"])

    def test_a_repeated_sku_keeps_the_first_line_and_is_reported(self):
        costs, duplicates = load_costs(
            ["sku", "cost"],
            [{"sku": "500001", "cost": "20.00"}, {"sku": "500001", "cost": "99.00"}])
        self.assertEqual(costs["500001"].cost, 20.00)
        self.assertEqual(duplicates, ["500001"])


# --------------------------------------------------------------------------- the join

class Join(unittest.TestCase):
    def test_a_variant_with_no_cost_line_is_set_aside(self):
        plan = plan_for([variant_row()],
                        [{"sku": "999999", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(len(plan.no_cost), 1)
        self.assertEqual(plan.matched, 0)

    def test_a_variant_with_no_sku_cannot_be_joined(self):
        plan = plan_for([variant_row(**{"Variant SKU": "", "Option1 Value": "Large"})],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(len(plan.no_sku), 1)

    def test_a_cost_line_with_no_variant_is_reported(self):
        plan = plan_for([variant_row()],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"},
                         {"sku": "999999", "cost": "5.00", "msrp": "20.00"}])
        self.assertEqual(plan.unused_costs, ["999999"])

    def test_rises_falls_and_unchanged_are_counted_separately(self):
        rows = [
            variant_row(**{"Variant SKU": "500001", "Variant Price": "38.00"}),
            variant_row(Handle="b", Title="B",
                        **{"Variant SKU": "500002", "Variant Price": "30.00"}),
            variant_row(Handle="c", Title="C",
                        **{"Variant SKU": "500003", "Variant Price": "30.00"}),
        ]
        costs = [
            {"sku": "500001", "cost": "20.00", "msrp": "52.00"},   # -> 41.60, rise
            {"sku": "500002", "cost": "9.00", "msrp": "26.00"},    # -> 20.80, fall
            {"sku": "500003", "cost": "18.00", "msrp": "37.50"},   # -> 30.00, same
        ]
        plan = plan_for(rows, costs)
        self.assertEqual(
            (len(plan.rises), len(plan.falls), len(plan.unchanged)), (1, 1, 1))

    def test_value_change_is_the_sum_of_the_moves(self):
        rows = [
            variant_row(**{"Variant SKU": "500001", "Variant Price": "38.00"}),
            variant_row(Handle="b", Title="B",
                        **{"Variant SKU": "500002", "Variant Price": "30.00"}),
        ]
        costs = [
            {"sku": "500001", "cost": "20.00", "msrp": "52.00"},  # +3.60
            {"sku": "500002", "cost": "9.00", "msrp": "26.00"},   # -9.20
        ]
        self.assertEqual(plan_for(rows, costs).value_change, -5.60)

    def test_movers_are_ordered_by_absolute_move_and_capped_at_five(self):
        rows, costs = [], []
        for index in range(7):
            sku = "5000{0:02d}".format(index)
            rows.append(variant_row(Handle="h{0}".format(index), Title="H",
                                    **{"Variant SKU": sku, "Variant Price": "100.00"}))
            # msrp 100 + 10*index, so price becomes 80 + 8*index
            costs.append({"sku": sku, "cost": "10.00",
                          "msrp": "{0:.2f}".format(100.0 + 10 * index)})
        movers = plan_for(rows, costs).movers()
        self.assertEqual(len(movers), 5)
        sizes = [abs(change.delta) for change in movers]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_an_unchanged_price_is_not_a_mover(self):
        plan = plan_for([variant_row(**{"Variant Price": "30.00"})],
                        [{"sku": "500001", "cost": "18.00", "msrp": "37.50"}])
        self.assertEqual(plan.movers(), [])


# --------------------------------------------------------------------------- the guard

class MaxChangeGuard(unittest.TestCase):
    def plan(self, old_price, cost, msrp="", max_change=sr.MAX_CHANGE):
        return plan_for([variant_row(**{"Variant Price": old_price})],
                        [{"sku": "500001", "cost": cost, "msrp": msrp}],
                        max_change=max_change)

    def test_a_price_moving_more_than_the_limit_is_held_back(self):
        """A supplier cost of 22.00 where 220.00 was meant: a 63% fall."""
        plan = self.plan("120.00", "22.00")
        self.assertEqual(len(plan.violations), 1)
        self.assertEqual(plan.changes, [])

    def test_a_price_moving_exactly_the_limit_is_allowed(self):
        """25.00 at cost 12.50 with no msrp gives 25.00, a fall of exactly 50%."""
        plan = self.plan("50.00", "12.50")
        self.assertEqual(plan.violations, [])
        self.assertEqual(len(plan.changes), 1)

    def test_a_rise_can_break_the_guard_too(self):
        plan = self.plan("10.00", "10.00")  # -> 20.00, a rise of 100%
        self.assertEqual(len(plan.violations), 1)

    def test_a_tighter_limit_catches_a_smaller_move(self):
        plan = self.plan("38.00", "20.00", "52.00", max_change=5)  # +9.5%
        self.assertEqual(len(plan.violations), 1)

    def test_a_looser_limit_lets_a_large_move_through(self):
        plan = self.plan("120.00", "22.00", max_change=80)
        self.assertEqual(plan.violations, [])

    def test_a_limit_of_zero_catches_any_move_but_not_an_unchanged_price(self):
        moved = self.plan("38.00", "20.00", "52.00", max_change=0)
        self.assertEqual(len(moved.violations), 1)
        same = self.plan("30.00", "18.00", "37.50", max_change=0)
        self.assertEqual(same.violations, [])

    def test_a_variant_with_no_current_price_cannot_breach_a_percentage(self):
        """There is no percentage move from nothing, so it is priced, not held back."""
        plan = plan_for([variant_row(**{"Variant Price": ""})],
                        [{"sku": "500001", "cost": "22.00", "msrp": ""}])
        self.assertEqual(plan.violations, [])
        self.assertEqual(len(plan.changes), 1)
        self.assertIsNone(plan.changes[0].pct)

    def test_a_held_back_row_is_left_out_of_the_value_change(self):
        rows = [
            variant_row(**{"Variant SKU": "500001", "Variant Price": "38.00"}),
            variant_row(Handle="b", Title="B",
                        **{"Variant SKU": "500002", "Variant Price": "120.00"}),
        ]
        costs = [
            {"sku": "500001", "cost": "20.00", "msrp": "52.00"},  # +3.60
            {"sku": "500002", "cost": "22.00", "msrp": ""},       # -76.00, held back
        ]
        plan = plan_for(rows, costs)
        self.assertEqual(plan.value_change, 3.60)
        self.assertEqual(len(plan.violations), 1)


# --------------------------------------------------------------------------- output file

class OutputFile(unittest.TestCase):
    def written(self, plan):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        try:
            sr.write_csv(path, plan)
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                return list(reader)
        finally:
            os.unlink(path)

    def test_the_header_is_exactly_the_columns_shopify_needs(self):
        plan = plan_for([variant_row()],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(self.written(plan)[0], sr.OUT_COLUMNS)

    def test_the_price_and_compare_at_are_the_rule_output(self):
        plan = plan_for([variant_row()],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(self.written(plan)[1][-2:], ["41.60", "52.00"])

    def test_the_sku_is_written_without_shopifys_apostrophe(self):
        plan = plan_for([variant_row(**{"Variant SKU": "'500001"})],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}])
        self.assertEqual(self.written(plan)[1][4], "500001")

    def test_compare_at_is_empty_when_it_would_not_be_above_the_price(self):
        plan = plan_for([variant_row()],
                        [{"sku": "500001", "cost": "20.00", "msrp": "52.00"}],
                        rule=sr.Rule(discount=0.0), max_change=1000)
        self.assertEqual(self.written(plan)[1][-1], "")

    def test_held_back_rows_are_not_in_the_file(self):
        rows = [
            variant_row(**{"Variant SKU": "500001", "Variant Price": "38.00"}),
            variant_row(Handle="b", Title="B",
                        **{"Variant SKU": "500002", "Variant Price": "120.00"}),
        ]
        costs = [
            {"sku": "500001", "cost": "20.00", "msrp": "52.00"},
            {"sku": "500002", "cost": "22.00", "msrp": ""},
        ]
        written = self.written(plan_for(rows, costs))
        self.assertEqual(len(written), 2)  # header plus the one safe row
        self.assertEqual(written[1][0], "demo-product")


# --------------------------------------------------------------------------- cli

class CommandLine(unittest.TestCase):
    def setUp(self):
        self.export = write_csv(EXPORT_COLUMNS, [
            variant_row(**{"Variant SKU": "500001", "Variant Price": "38.00"}),
            variant_row(Handle="b", Title="B",
                        **{"Variant SKU": "500002", "Variant Price": "120.00"}),
        ])
        self.costs = write_csv(["sku", "cost", "msrp"], [
            {"sku": "500001", "cost": "20.00", "msrp": "52.00"},
            {"sku": "500002", "cost": "22.00", "msrp": ""},
        ])
        handle, self.out = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        self._streams = (sys.stdout, sys.stderr)
        self._quiet = open(os.devnull, "w")
        sys.stdout = sys.stderr = self._quiet

    def tearDown(self):
        sys.stdout, sys.stderr = self._streams
        self._quiet.close()
        for path in (self.export, self.costs, self.out):
            if os.path.exists(path):
                os.unlink(path)

    def test_exit_code_is_one_when_a_row_breaks_the_guard(self):
        self.assertEqual(sr.main([self.export, self.costs, "-o", self.out]), 1)

    def test_exit_code_is_zero_when_nothing_breaks_the_guard(self):
        self.assertEqual(
            sr.main([self.export, self.costs, "-o", self.out, "--max-change", "200"]), 0)

    def test_the_file_is_written_and_holds_the_safe_rows(self):
        sr.main([self.export, self.costs, "-o", self.out])
        with open(self.out, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows), 2)

    def test_a_dry_run_writes_nothing(self):
        before = os.path.getsize(self.out)
        sr.main([self.export, self.costs])
        self.assertEqual(os.path.getsize(self.out), before)

    def test_a_percentage_given_to_discount_is_refused(self):
        with self.assertRaises(SystemExit):
            sr.main([self.export, self.costs, "--discount", "20"])

    def test_a_zero_multiple_is_refused(self):
        with self.assertRaises(SystemExit):
            sr.main([self.export, self.costs, "--min-multiple", "0"])

    def test_a_negative_max_change_is_refused(self):
        with self.assertRaises(SystemExit):
            sr.main([self.export, self.costs, "--max-change", "-5"])

    def test_the_cli_multiples_reach_the_rule(self):
        sr.main([self.export, self.costs, "-o", self.out,
                 "--fallback-multiple", "10", "--max-change", "1000"])
        with open(self.out, newline="", encoding="utf-8") as fh:
            rows = {row["Variant SKU"]: row for row in csv.DictReader(fh)}
        self.assertEqual(rows["500002"]["Variant Price"], "176.00")  # 22 x 10 x 0.8


# --------------------------------------------------------------------------- examples

class ShippedExamples(unittest.TestCase):
    """The sample files are documentation, so they are allowed to be wrong only once."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.export = os.path.join(root, "examples", "sample_export.csv")
        self.costs = os.path.join(root, "examples", "sample_costs.csv")

    def test_the_samples_demonstrate_one_of_everything(self):
        variants, products = sr.load_export(self.export)
        costs, _ = sr.load_costs(self.costs)
        plan = sr.build_plan(variants, products, costs, [], sr.Rule(), sr.MAX_CHANGE)
        self.assertTrue(plan.rises, "no price rises in the sample")
        self.assertTrue(plan.falls, "no price falls in the sample")
        self.assertTrue(plan.unchanged, "no unchanged price in the sample")
        self.assertTrue(plan.no_cost, "no unmatched variant in the sample")
        self.assertTrue(plan.unused_costs, "no unmatched cost line in the sample")
        self.assertTrue(plan.violations, "no guard violation in the sample")
        self.assertTrue(any("below" in c.basis for c in plan.changes),
                        "no msrp-below-the-multiple case in the sample")


if __name__ == "__main__":
    unittest.main()
