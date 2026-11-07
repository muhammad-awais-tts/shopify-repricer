# shopify-repricer

Turn a supplier cost list into a Shopify price import, and see what it would do first.

A supplier sends a cost list. Shopify gives you a product export. Between the two sits an
afternoon of spreadsheet work: match the SKUs, apply whatever margin rule you use, work out
the compare-at price, and paste the results into a file Shopify will accept without
rewriting the rest of the catalogue.

This does the join, applies one pricing rule, and writes a CSV containing **only the columns
Shopify needs to update a price**. Before it writes anything it prints what would change,
and it refuses to write a row whose price moves further than a threshold you set.

No API key. No app install. No account access. Nothing uploaded anywhere — it runs on two
files you already have, with the Python standard library and nothing else.

```
==========================================================================
  Shopify repricer
==========================================================================
  rule   retail = msrp, but never below cost x 1.875
         no msrp: retail = cost x 2.5
         price  = retail less 20%
  guard  refuse any price moving more than 50%
--------------------------------------------------------------------------
       9  variants in the export (7 products)
       7  matched to a cost line
       1  in the export with no cost line for that SKU
       1  in the export with no SKU, nothing to join on
       1  cost lines with no matching variant
--------------------------------------------------------------------------
       1  prices rise
       4  prices fall
       1  unchanged
  -11.00  change in catalogue value across the 6 rows to write
--------------------------------------------------------------------------
  Biggest movers
    handle                   variant       now         new     delta  change
    harborline-field-cap     One Size    25.00 ->    21.00     -4.00  -16.0%
    fernwood-trail-bottle    750 ml      36.00 ->    32.00     -4.00  -11.1%
    harborline-canvas-tote   One Size    38.00 ->    41.60     +3.60   +9.5%
    fernwood-ceramic-mug     500 ml      29.00 ->    25.60     -3.40  -11.7%
    fernwood-ceramic-mug     350 ml      24.00 ->    20.80     -3.20  -13.3%
--------------------------------------------------------------------------
  In the export, no cost line  400701
  In the cost list, no variant 400999
--------------------------------------------------------------------------
  ! 1 row moves more than 50% — not written
    A wrong cost line is the usual cause. Check it before importing.
    tallgrass-wool-throw     One Size   120.00 ->    44.00    -76.00  -63.3%
--------------------------------------------------------------------------
  6 rows written to updated.csv
  Open it and read it before importing it at Products -> Import.
==========================================================================
```

That run is the two sample files in `examples/`, and every line of it is worth reading
before the import. One price rises, four fall, one is already correct. One variant in the
export has no cost line, one cost line has no variant, one variant has no SKU to join on at
all — and one row was held back, because a supplier who types `22.00` where they meant
`220.00` would otherwise have taken a 120.00 blanket down to 44.00.

## Install

There is nothing to install.

```bash
curl -O https://raw.githubusercontent.com/muhammad-awais-tts/shopify-repricer/main/shopify_repricer.py
```

Python 3.8 or newer. No packages.

## Use

Get the export: **Shopify admin → Products → Export → All products → Plain CSV**. Shopify
emails you the file.

The cost list is whatever your supplier sends, as long as it is a CSV with a SKU column and a
cost column. An MSRP column is used if it is there.

```bash
# see what would happen, write nothing
python3 shopify_repricer.py export.csv costs.csv

# write the import file
python3 shopify_repricer.py export.csv costs.csv -o updated.csv
```

Then read `updated.csv`, and if it looks right, import it at **Products → Import**.

The output file for the run above is a header and six rows:

```csv
Handle,Title,Option1 Name,Option1 Value,Variant SKU,Variant Price,Variant Compare At Price
harborline-canvas-tote,Canvas Day Tote,Size,One Size,400101,41.60,52.00
fernwood-ceramic-mug,Ceramic Pour Mug,Size,350 ml,400201,20.80,26.00
fernwood-ceramic-mug,Ceramic Pour Mug,Size,500 ml,400202,25.60,32.00
tallgrass-linen-napkins,Linen Dinner Napkins,Set,Set of 4,400301,30.00,37.50
harborline-field-cap,Waxed Field Cap,Size,One Size,400401,21.00,26.25
fernwood-trail-bottle,Trail Water Bottle,Size,750 ml,400501,32.00,40.00
```

Those seven columns are the whole file on purpose. Re-import a full export and it carries
descriptions, image URLs, inventory counts, SEO fields and publication status with it, and
every one of those is something that can be overwritten with whatever happened to be in the
file. This import carries a handle and the option values that identify the variant, the title
that belongs beside them, and two prices. There is nothing else in it to get wrong.

## The pricing rule

```
retail = msrp, unless msrp is below cost x 1.875, in which case cost x 1.875
         (no msrp at all: cost x 2.50)
price  = retail less 20%
```

Both are rounded to two decimal places. `retail` is written to **Variant Compare At Price**
and `price` to **Variant Price**, so the product sits at a visible discount against a retail
price you can defend.

The `1.875` floor exists because supplier MSRPs are not always real. A supplier who lists a
$19 MSRP on a $14 item is describing their own margin, not a price you can sell at and still
pay for ads, returns and the platform's cut. When the MSRP is too low to support the
discount, the multiple takes over and the MSRP is ignored.

Every number in that rule is a flag, so if your margins are different, say so on the command
line rather than editing the file.

## Flags

| Flag | Default | What it does |
|---|---|---|
| `-o`, `--out PATH` | none | Write the Shopify import CSV here. Omit it and nothing is written; you get the summary only. |
| `--min-multiple N` | `1.875` | The lowest retail price the tool will claim, as a multiple of cost. An MSRP below this is replaced by it. |
| `--fallback-multiple N` | `2.50` | The retail multiple used when the cost list has no MSRP column, or the MSRP cell is empty. |
| `--discount FRACTION` | `0.20` | How far below retail the selling price sits. A fraction between 0 and 1; `0.20`, not `20`. At `0` the compare-at column is left empty rather than written equal to the price. |
| `--round-99` | off | Drop each price to the nearest `.99` at or below it, so `41.60` becomes `40.99`. Applies to the price, not the compare-at. Prices under `0.99` are left alone. |
| `--max-change PCT` | `50` | Refuse to write any row whose price moves more than this percentage, and list it as a warning instead. |

Exit code is `1` if any row broke the `--max-change` guard and `0` otherwise, so it works as a
gate in CI or in a cron job that repriced the catalogue overnight and should wake you up if
the cost file was wrong.

## Why the max-change guard matters

A repricing script is the one piece of automation in a store that can do real damage without
failing. Everything it writes is a valid price. Shopify will accept all of it. The orders
that follow are real orders at the wrong price, and you find out when the margin report comes
in a fortnight later.

Almost every bad price has the same cause: one line in the supplier file. A decimal point in
the wrong place, a price quoted per case instead of per unit, a currency that changed without
anyone saying so, a blank cell read as zero. None of those look like errors. They look like a
cost, and the rule prices them obediently.

A percentage guard catches them because real cost movements are small and bad ones are not. A
supplier raising prices moves them 4%, or 11%. A supplier who typed `22.00` instead of
`220.00` moves one by 63%, and that is the only row in the file you actually need to look at.
Held-back rows are kept out of the output file entirely, so the import is safe to run while
you go and ask about the ones that are not.

Set it tight when you are repricing a catalogue you know well:

```bash
python3 shopify_repricer.py export.csv costs.csv -o updated.csv --max-change 15
```

## The cost list

Suppliers do not agree on column names, so these are all accepted, case-insensitively and
ignoring underscores, hyphens and extra spaces:

| Meaning | Accepted headers |
|---|---|
| SKU | `sku`, `variant sku`, `item sku`, `supplier sku`, `product sku` |
| Cost | `cost`, `cost price`, `cost per item`, `unit cost`, `wholesale`, `wholesale price` |
| MSRP | `msrp`, `retail`, `retail price`, `list price`, `rrp` |

Only the SKU and the cost are required. Currency symbols and thousands separators are
stripped, so `$1,250.00` is a cost and not a reason to drop the row. A cost cell that is not a
number at all is skipped, and if the same SKU appears twice the first line is used and the
repeat is counted in the summary.

If the file is missing a SKU or cost column the tool stops and prints the headers it did find,
rather than quietly matching nothing.

## Notes on the export format

Shopify's CSV has three habits that break a naive join. All three are handled here.

- **Numeric SKUs come out as `'400201`**, with a leading apostrophe, so spreadsheets do not
  turn them into numbers. Join on them as they are and nothing matches the supplier file.
  SKUs are stripped on both sides before the join, and written back to the import file without
  the apostrophe.
- **One product is several rows.** Variants, extra images and extra option values each get
  their own row, carrying only the handle. Product-level fields — `Title`, `Option1 Name` —
  appear on whichever row came first and are blank on the rest. Those are carried forward
  from the first row of the group that has them, because the import file needs a title on
  every row it contains. Rows that carry no price, no SKU and no option value are image rows,
  not variants, and are left out.
- **An empty compare-at price exports as `0`**, which means "no sale price", not "free". It is
  read as absent, so a product that was never on sale is not reported as having had its
  discount removed.

One more thing about the output rather than the input: the compare-at column is written empty
whenever the retail price is not above the selling price. A compare-at equal to or below the
price shows the shopper a discount that is not one, and it is better to have no compare-at
than a dishonest one.

## What this does not do

It does not talk to Shopify, so it cannot change your store — it reads two files and writes a
third. It does not decide which products to reprice; it prices everything it can match, and
you edit the output file if you disagree. It does not handle multi-currency price lists,
per-market pricing, or anything beyond `Option1`, which covers single and simple
multi-variant products and not much else.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

65 tests. Every rule has a case that trips it and a near miss that does not — the MSRP exactly
on the 1.875 floor, the price that falls by exactly the 50% guard, the compare-at of zero, the
SKU that differs only by Shopify's apostrophe, the variant with no price at all, which has no
percentage move to measure and so cannot breach the guard. The sample files in `examples/` are
checked too, because they are documentation and documentation drifts.

## Licence

MIT.
