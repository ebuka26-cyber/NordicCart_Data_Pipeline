"""Generate the NordicCart Power BI Project (PBIP: TMDL semantic model + PBIR report) from one readable spec.

    python powerbi/build_pbip.py [--local | --analytics-dir <folder>]

Writes powerbi/NordicCart.pbip, powerbi/NordicCart.SemanticModel/ and powerbi/NordicCart.Report/ (overwritten). Open
NordicCart.pbip in Power BI Desktop and click Refresh. The data source is the hourly ETL's published analytics CSVs
(output/etl/analytics); the folder is the Power Query parameter AnalyticsFolder. Power Query has no relative paths, so
the committed project uses the neutral placeholder PORTABLE_ANALYTICS_DIR (no machine-specific path); --local writes this
clone's absolute folder instead (a working copy for this machine: do not commit it), and Desktop's
Transform data > Edit parameters changes it at any time.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
NAME = "NordicCart"
NS = uuid.UUID("6f1d3c52-6a4e-4d7b-9a43-3f5a0c7e2b10")
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
BASE_THEME = "Fluent2-CY26SU10"
PORTABLE_ANALYTICS_DIR = Path(r"C:\NordicCart_Data_Pipeline\output\etl\analytics")   # neutral default for the repository
IMPORT_VERSION ={"visual": "2.13.0", "report": "3.4.0", "page": "2.3.1"}   # as written by Power BI Desktop 2.159


def tag(*parts: str) -> str:
    """Stable lineage tag / object name."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def q(name: str) -> str:
    """TMDL object name, quoted when needed."""
    return name if name.replace("_", "").isalnum() else "'" + name.replace("'", "''") + "'"


# ============================================================================ semantic model spec
def csv_query(file: str, types: list[tuple[str, str]], extra: str = "") -> str:
    typed = ", ".join(f'{{"{c}", {t}}}' for c, t in types)
    return f'''let
    Source = Csv.Document(File.Contents(AnalyticsFolder & "\\{file}"), [Delimiter = ",", Columns = {len(types)}, Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(Promoted, {{{typed}}}, "en-US"){extra}
in
    Result'''


SALES_TYPES = [("fulfilment_date_local", "type date"), ("market", "type text"), ("channel", "type text"),
               ("currency", "type text"), ("orders", "Int64.Type"), ("order_lines", "Int64.Type"),
               ("units", "Int64.Type"), ("revenue_gross_local", "Currency.Type"), ("vat_local", "Currency.Type"),
               ("revenue_net_local", "Currency.Type"), ("revenue_gross_eur", "Currency.Type"),
               ("revenue_net_eur", "Currency.Type"), ("orders_without_eur_rate", "Int64.Type")]
ORDERS_TYPES = [("month_local", "type text"), ("market", "type text"), ("channel", "type text"),
                ("status", "type text"), ("orders", "Int64.Type")]
INVENTORY_TYPES = [("location_code", "type text"), ("location_type", "type text"), ("market", "type text"),
                   ("sku", "type text"), ("category", "type text"), ("on_hand", "Int64.Type"),
                   ("reserved", "Int64.Type"), ("reorder_point", "Int64.Type")]

M_SALES = csv_query("fact_sales_daily.csv", SALES_TYPES, ",\n    Result = Typed")
M_ORDERS = csv_query("orders_monthly.csv", ORDERS_TYPES, ''',
    Result = Table.AddColumn(Typed, "month_start", each Date.FromText([month_local] & "-01", [Format = "yyyy-MM-dd"]), type date)''')
M_INVENTORY = csv_query("inventory_current.csv", INVENTORY_TYPES, ''',
    Available = Table.AddColumn(Typed, "available", each [on_hand] - [reserved], Int64.Type),
    LowStock = Table.AddColumn(Available, "low_stock", each [available] <= [reorder_point], type logical),
    Result = Table.AddColumn(LowStock, "stock_status", each if [available] <= 0 then "Out of stock" else if [low_stock] then "Low stock" else "In stock", type text)''')
M_COUNTRY = '''let
    Result = #table(type table [country_code = text, Country = text, Currency = text],
        {{"DK", "Denmark", "DKK"}, {"FI", "Finland", "EUR"}, {"NO", "Norway", "NOK"}, {"SE", "Sweden", "SEK"}})
in
    Result'''
M_CHANNEL = '''let
    Result = #table(type table [channel_code = text, Channel = text, channel_order = Int64.Type],
        {{"online", "Online", 1}, {"in_store", "In store", 2}})
in
    Result'''
M_FRESHNESS = '''let
    Files = Folder.Files(AnalyticsFolder),
    Published = Table.SelectRows(Files, each List.Contains({"fact_sales_daily.csv", "orders_monthly.csv", "inventory_current.csv"}, [Name])),
    Result = Table.TransformColumnTypes(Table.RenameColumns(Table.SelectColumns(Published, {"Name", "Date modified"}), {{"Name", "file"}, {"Date modified", "published_local"}}), {{"published_local", type datetime}})
in
    Result'''
DAX_DATE = '''VAR _first = MIN ( MIN ( FactSales[fulfilment_date_local] ), MIN ( FactOrdersMonthly[month_start] ) )
VAR _last = MAX ( MAX ( FactSales[fulfilment_date_local] ), MAX ( FactOrdersMonthly[month_start] ) )
RETURN
    ADDCOLUMNS (
        CALENDAR ( DATE ( YEAR ( _first ), 1, 1 ), DATE ( YEAR ( _last ), 12, 31 ) ),
        "Year", YEAR ( [Date] ),
        "Quarter", "Q" & QUARTER ( [Date] ),
        "Month Number", MONTH ( [Date] ),
        "Month", FORMAT ( [Date], "mmm" ),
        "Year Month", FORMAT ( [Date], "yyyy-mm" ),
        "Year Month Key", YEAR ( [Date] ) * 100 + MONTH ( [Date] ),
        "Month Start", DATE ( YEAR ( [Date] ), MONTH ( [Date] ), 1 )
    )'''

DT = {"type date": "dateTime", "type text": "string", "Int64.Type": "int64", "Currency.Type": "decimal",
      "type logical": "boolean", "type datetime": "dateTime"}
FMT = {"dateTime": "yyyy-mm-dd", "int64": "#,0", "decimal": "#,0.00"}

EUR0, EUR2, INT, PCT, DEC2 = "€#,0", "€#,0.00", "#,0", "0.0%", "0.00"
LOW = "FactInventory[low_stock] = TRUE ()"
# the selected dates, cut at the last date that has data (so a year-to-date compares with the same dates a year earlier)
SALES_DATES = ("VAR _last = CALCULATE ( MAX ( FactSales[fulfilment_date_local] ), REMOVEFILTERS () ) "
               "VAR _dates = CALCULATETABLE ( VALUES ( DimDate[Date] ), KEEPFILTERS ( DimDate[Date] <= _last ) )")
ORDER_DATES = ("VAR _last = EOMONTH ( CALCULATE ( MAX ( FactOrdersMonthly[month_start] ), REMOVEFILTERS () ), 0 ) "
               "VAR _dates = CALCULATETABLE ( VALUES ( DimDate[Date] ), KEEPFILTERS ( DimDate[Date] <= _last ) )")
# (table, name, DAX, format, display folder, description)
MEASURES = [
    ("FactSales", "Gross Revenue EUR", "SUM ( FactSales[revenue_gross_eur] )", EUR0, "Sales",
     "Fulfilled-order revenue incl. VAT, converted at the fulfilment-date EUR rate (ETL)."),
    ("FactSales", "Net Revenue EUR", "SUM ( FactSales[revenue_net_eur] )", EUR0, "Sales", "Revenue excl. VAT, EUR."),
    ("FactSales", "VAT EUR", "[Gross Revenue EUR] - [Net Revenue EUR]", EUR0, "Sales", "VAT, EUR."),
    ("FactSales", "Fulfilled Orders", "SUM ( FactSales[orders] )", INT, "Sales",
     "Orders fulfilled, by local fulfilment date."),
    ("FactSales", "Order Lines", "SUM ( FactSales[order_lines] )", INT, "Sales", "Lines of fulfilled orders."),
    ("FactSales", "Units Sold", "SUM ( FactSales[units] )", INT, "Sales", "Units of fulfilled orders."),
    ("FactSales", "Average Order Value EUR", "DIVIDE ( [Gross Revenue EUR], [Fulfilled Orders] )", EUR2, "Sales",
     "Gross revenue per fulfilled order, EUR."),
    ("FactSales", "Units per Order", "DIVIDE ( [Units Sold], [Fulfilled Orders] )", DEC2, "Sales",
     "Units per fulfilled order."),
    ("FactSales", "Gross Revenue Local",
     "IF ( HASONEVALUE ( FactSales[currency] ), SUM ( FactSales[revenue_gross_local] ) )", "#,0", "Sales",
     "Gross revenue in local currency; blank when several currencies are in context."),
    ("FactSales", "Share of Gross Revenue",
     "DIVIDE ( [Gross Revenue EUR], CALCULATE ( [Gross Revenue EUR], ALL ( DimCountry ), ALL ( DimChannel ) ) )",
     PCT, "Sales", "Share of all countries and channels in the selected period."),
    ("FactSales", "Gross Revenue EUR PY", f"{SALES_DATES} RETURN "
     "CALCULATE ( [Gross Revenue EUR], SAMEPERIODLASTYEAR ( _dates ) )", EUR0, "Sales",
     "Gross revenue one year earlier, over the selected dates up to the last fulfilment date (like for like)."),
    ("FactSales", "Gross Revenue YoY %",
     f"VAR _first = CALCULATE ( MIN ( FactSales[fulfilment_date_local] ), REMOVEFILTERS () ) {SALES_DATES} "
     "VAR _pyStart = MINX ( SAMEPERIODLASTYEAR ( _dates ), DimDate[Date] ) VAR _py = [Gross Revenue EUR PY] "
     "RETURN IF ( _pyStart >= _first && NOT ISBLANK ( _py ), DIVIDE ( [Gross Revenue EUR] - _py, _py ) )",
     PCT, "Sales", "Year-over-year change; blank when the prior-year period starts before the data (Nov 2024)."),
    ("FactSales", "Orders Without EUR Rate", "SUM ( FactSales[orders_without_eur_rate] )", INT, "Sales",
     "Fulfilled orders that had no EUR rate (expected 0)."),
    ("FactOrdersMonthly", "Orders Placed", "SUM ( FactOrdersMonthly[orders] )", INT, "Orders",
     "Orders placed, by local placement month, any status."),
    ("FactOrdersMonthly", "Orders Fulfilled",
     'CALCULATE ( [Orders Placed], FactOrdersMonthly[status] = "fulfilled" )', INT, "Orders",
     "Placed orders now fulfilled (placement-month basis)."),
    ("FactOrdersMonthly", "Orders Cancelled",
     'CALCULATE ( [Orders Placed], FactOrdersMonthly[status] = "cancelled" )', INT, "Orders", "Placed orders cancelled."),
    ("FactOrdersMonthly", "Orders Payment Failed",
     'CALCULATE ( [Orders Placed], FactOrdersMonthly[status] = "payment_failed" )', INT, "Orders",
     "Placed orders whose payment failed."),
    ("FactOrdersMonthly", "Orders In Progress",
     'CALCULATE ( [Orders Placed], NOT FactOrdersMonthly[status] IN { "fulfilled", "cancelled", "payment_failed" } )',
     INT, "Orders", "Placed orders not yet in a final status (e.g. confirmed)."),
    ("FactOrdersMonthly", "Fulfilment Rate", "DIVIDE ( [Orders Fulfilled], [Orders Placed] )", "0.00%", "Orders",
     "Fulfilled / placed."),
    ("FactOrdersMonthly", "Cancellation Rate", "DIVIDE ( [Orders Cancelled], [Orders Placed] )", "0.00%", "Orders",
     "Cancelled / placed."),
    ("FactOrdersMonthly", "Payment Failure Rate", "DIVIDE ( [Orders Payment Failed], [Orders Placed] )", "0.00%",
     "Orders", "Payment failed / placed."),
    ("FactOrdersMonthly", "Orders Placed PY", f"{ORDER_DATES} RETURN "
     "CALCULATE ( [Orders Placed], SAMEPERIODLASTYEAR ( _dates ) )", INT, "Orders",
     "Orders placed one year earlier, over the selected months up to the last placement month."),
    ("FactOrdersMonthly", "Orders Placed YoY %",
     f"VAR _first = CALCULATE ( MIN ( FactOrdersMonthly[month_start] ), REMOVEFILTERS () ) {ORDER_DATES} "
     "VAR _pyStart = MINX ( SAMEPERIODLASTYEAR ( _dates ), DimDate[Date] ) VAR _py = [Orders Placed PY] "
     "RETURN IF ( _pyStart >= _first && NOT ISBLANK ( _py ), DIVIDE ( [Orders Placed] - _py, _py ) )", PCT,
     "Orders", "Year-over-year change of orders placed; the latest month is partial in both years' comparison."),
    ("FactInventory", "Units On Hand", "SUM ( FactInventory[on_hand] )", INT, "Inventory", "Current units on hand."),
    ("FactInventory", "Units Reserved", "SUM ( FactInventory[reserved] )", INT, "Inventory",
     "Units reserved for open orders."),
    ("FactInventory", "Units Available", "SUM ( FactInventory[available] )", INT, "Inventory", "On hand - reserved."),
    ("FactInventory", "Stock Positions", "COUNTROWS ( FactInventory )", INT, "Inventory",
     "Location x SKU positions."),
    ("FactInventory", "Low Stock Positions", f"CALCULATE ( COUNTROWS ( FactInventory ), {LOW} )", INT, "Inventory",
     "Positions with available <= reorder point."),
    ("FactInventory", "Out of Stock Positions",
     "CALCULATE ( COUNTROWS ( FactInventory ), FactInventory[available] <= 0 )", INT, "Inventory",
     "Positions with nothing available."),
    ("FactInventory", "Low Stock Rate", "DIVIDE ( [Low Stock Positions], [Stock Positions] )", PCT, "Inventory",
     "Low-stock positions / positions."),
    ("FactInventory", "SKUs with Low Stock",
     f"CALCULATE ( DISTINCTCOUNT ( FactInventory[sku] ), {LOW} )", INT, "Inventory",
     "Distinct SKUs low somewhere."),
    ("FactInventory", "Low Stock Available", f"CALCULATE ( [Units Available], KEEPFILTERS ( {LOW} ) )", INT,
     "Inventory", "Units available, low-stock positions only (blank otherwise)."),
    ("FactInventory", "Low Stock Reorder Point",
     f"CALCULATE ( SUM ( FactInventory[reorder_point] ), KEEPFILTERS ( {LOW} ) )", INT, "Inventory",
     "Reorder point, low-stock positions only."),
    ("FactInventory", "Reorder Shortfall Units",
     f"SUMX ( FILTER ( FactInventory, {LOW} ), FactInventory[reorder_point] - FactInventory[available] )", INT,
     "Inventory", "Units needed to bring low-stock positions back to their reorder point."),
    ("DataFreshness", "Data As Of", "MAX ( DataFreshness[published_local] )", "yyyy-mm-dd hh:nn", "Status",
     "Latest publication time of the analytics files (file modification time, local time of the refreshing machine)."),
]

# table -> (kind, source, [(column, dataType, hidden, extra lines)])
HIDE_NUMERIC = True
TABLES = {
    "FactSales": ("m", M_SALES, [(c, DT[t], HIDE_NUMERIC and DT[t] in ("int64", "decimal")) for c, t in SALES_TYPES]),
    "FactOrdersMonthly": ("m", M_ORDERS, [(c, DT[t], HIDE_NUMERIC and DT[t] == "int64") for c, t in ORDERS_TYPES]
                          + [("month_start", "dateTime", True)]),
    "FactInventory": ("m", M_INVENTORY, [(c, DT[t], False) for c, t in INVENTORY_TYPES]
                      + [("available", "int64", False), ("low_stock", "boolean", False),
                         ("stock_status", "string", False)]),
    "DimCountry": ("m", M_COUNTRY, [("country_code", "string", False), ("Country", "string", False),
                                    ("Currency", "string", False)]),
    "DimChannel": ("m", M_CHANNEL, [("channel_code", "string", True), ("Channel", "string", False),
                                    ("channel_order", "int64", True)]),
    "DataFreshness": ("m", M_FRESHNESS, [("file", "string", False), ("published_local", "dateTime", False)]),
    "DimDate": ("calculated", DAX_DATE, [("Date", "dateTime", False), ("Year", "int64", False),
                                         ("Quarter", "string", False), ("Month Number", "int64", True),
                                         ("Month", "string", False), ("Year Month", "string", False),
                                         ("Year Month Key", "int64", True), ("Month Start", "dateTime", False)]),
}
SORT_BY = {("DimDate", "Month"): "Month Number", ("DimDate", "Year Month"): "Year Month Key",
           ("DimChannel", "Channel"): "channel_order"}
FORMATS = {("DataFreshness", "published_local"): "yyyy-mm-dd hh:nn", ("DimDate", "Year"): "0"}
RELATIONSHIPS = [("FactSales", "fulfilment_date_local", "DimDate", "Date"),
                 ("FactOrdersMonthly", "month_start", "DimDate", "Date"),
                 ("FactSales", "market", "DimCountry", "country_code"),
                 ("FactOrdersMonthly", "market", "DimCountry", "country_code"),
                 ("FactInventory", "market", "DimCountry", "country_code"),
                 ("FactSales", "channel", "DimChannel", "channel_code"),
                 ("FactOrdersMonthly", "channel", "DimChannel", "channel_code")]


def indent(text: str, tabs: int) -> str:
    return "\n".join("\t" * tabs + line if line else line for line in text.splitlines())


def table_tmdl(name: str) -> str:
    kind, source, columns = TABLES[name]
    out = [f"table {name}"]
    if name == "DimDate":
        out.append("\tdataCategory: Time")
    out += [f"\tlineageTag: {tag('table', name)}", ""]
    for t, m, dax, fmt, folder, desc in MEASURES:
        if t != name:
            continue
        out += [f"\t/// {desc}", f"\tmeasure {q(m)} = {dax}", f"\t\tformatString: {fmt}",
                f"\t\tdisplayFolder: {folder}", f"\t\tlineageTag: {tag('measure', name, m)}", ""]
    for col, dtype, hidden in columns:
        numeric = dtype in ("int64", "decimal") and not col.endswith(("_order", "Key", "Number")) and name != "DimDate"
        out += [f"\tcolumn {q(col)}", f"\t\tdataType: {dtype}"]
        if name == "DimDate" and col == "Date":
            out.append("\t\tisKey")
        fmt = FORMATS.get((name, col), FMT.get(dtype))
        if fmt:
            out.append(f"\t\tformatString: {fmt}")
        if hidden:
            out.append("\t\tisHidden")
        out += [f"\t\tlineageTag: {tag('column', name, col)}", f"\t\tsummarizeBy: {'sum' if numeric else 'none'}"]
        if kind == "calculated":
            out += ["\t\tisNameInferred", f"\t\tsourceColumn: [{col}]"]
        else:
            out.append(f"\t\tsourceColumn: {col}")
        if (name, col) in SORT_BY:
            out.append(f"\t\tsortByColumn: {q(SORT_BY[(name, col)])}")
        out += ["", "\t\tannotation SummarizationSetBy = Automatic", ""]
    out += [f"\tpartition {name} = {'calculated' if kind == 'calculated' else 'm'}", "\t\tmode: import",
            "\t\tsource =", indent(source, 3), ""]
    out.append(f"\tannotation PBI_ResultType = Table")
    return "\n".join(out) + "\n"


def write_model(root: Path, analytics_dir: Path) -> None:
    d = root / f"{NAME}.SemanticModel"
    if d.exists():
        shutil.rmtree(d)
    (d / "definition" / "tables").mkdir(parents=True)
    dump(d / "definition.pbism", {"$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
                                  "version": "4.2", "settings": {}})
    text(d / "definition" / "database.tmdl", "database\n\tcompatibilityLevel: 1606\n")
    order = ["AnalyticsFolder", *[t for t in TABLES if TABLES[t][0] == "m"]]
    text(d / "definition" / "model.tmdl", "\n".join([
        "model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tdiscourageImplicitMeasures", "\tsourceQueryCulture: en-US", "\tdataAccessOptions", "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull", "",
        f"annotation PBI_QueryOrder = {json.dumps(order)}", "", "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {t}" for t in TABLES], ""]))
    text(d / "definition" / "expressions.tmdl", "\n".join([
        f'expression AnalyticsFolder = "{analytics_dir}" meta [IsParameterQuery = true, Type = "Text", '
        f'IsParameterQueryRequired = true]',
        f"\tlineageTag: {tag('expression', 'AnalyticsFolder')}", "",
        "\tannotation PBI_ResultType = Text", ""]))
    rel = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rel += [f"relationship {tag('rel', ft, fc, tt, tc)}", f"\tfromColumn: {ft}.{q(fc)}",
                f"\ttoColumn: {tt}.{q(tc)}", ""]
    text(d / "definition" / "relationships.tmdl", "\n".join(rel))
    for t in TABLES:
        text(d / "definition" / "tables" / f"{t}.tmdl", table_tmdl(t))


# ============================================================================ report spec
def lit(v) -> dict:
    if isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (int, float)):
        s = f"{v}D"
    else:
        s = "'" + str(v).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": s}}}


def col(table: str, column: str) -> tuple:
    return ("Column", table, column)


def mea(table: str, measure: str) -> tuple:
    return ("Measure", table, measure)


def field(f: tuple) -> dict:
    kind, table, prop = f
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": prop}}


def projections(fields: list[tuple]) -> dict:
    out = []
    for f in fields:
        p = {"field": field(f[:3]), "queryRef": f"{f[1]}.{f[2]}", "nativeQueryRef": f[2]}
        if len(f) > 3:
            p["displayName"] = f[3]
        out.append(p)
    return {"projections": out}


class Page:
    def __init__(self, name: str, title: str, subtitle: str):
        self.name, self.title, self.subtitle, self.visuals = name, title, subtitle, []

    def add(self, vtype: str, x, y, w, h, roles: dict | None = None, title: str | None = None,
            objects: dict | None = None, sort: tuple | None = None, container: dict | None = None) -> None:
        visual = {"visualType": vtype}
        if roles:
            visual["query"] = {"queryState": {r: projections(f) for r, f in roles.items()}}
            if sort:
                visual["query"]["sortDefinition"] = {"sort": [{"field": field(sort[0]), "direction": sort[1]}],
                                                     "isDefaultSort": False}
        if objects:
            visual["objects"] = objects
        vco = {"subTitle": [{"properties": {"show": lit(False)}}], **(container or {})}
        if title:
            vco["title"] = [{"properties": {"show": lit(True), "text": lit(title), "fontSize": lit(11),
                                            "bold": lit(True), "fontColor": {"solid": {"color": lit("#13304A")}}}}]
        elif vtype in ("card", "slicer"):
            vco["title"] = [{"properties": {"show": lit(False)}}]
        if vtype == "textbox":
            vco.update({k: [{"properties": {"show": lit(False)}}] for k in ("background", "border", "dropShadow",
                                                                             "visualHeader")})
        if vco:
            visual["visualContainerObjects"] = vco
        visual["drillFilterOtherVisuals"] = True
        n = len(self.visuals)
        self.visuals.append({"$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.9.0/schema.json",
                             "name": tag("visual", self.name, str(n)).replace("-", "")[:20],
                             "position": {"x": x, "y": y, "z": n * 1000, "height": h, "width": w, "tabOrder": n * 1000},
                             "visual": visual})


def textbox(page: Page, x, y, w, h, paragraphs: list[tuple[str, dict]]) -> None:
    page.add("textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": v, "textStyle": s}]} for v, s in paragraphs]}}]})


def slicer(page: Page, x, w, f: tuple, label: str, mode: str = "Dropdown") -> None:
    page.add("slicer", x, 76, w, 60, {"Values": [(*f, label)]}, objects={"data": [{"properties": {"mode": lit(mode)}}]})


def cards(page: Page, measures: list[tuple]) -> None:
    gap, x0, width = 12, 24, 1232
    w = (width - gap * (len(measures) - 1)) / len(measures)
    for i, m in enumerate(measures):
        page.add("card", round(x0 + i * (w + gap)), 144, round(w), 92, {"Values": [m]},
                 objects={"labels": [{"properties": {"fontSize": lit(22)}}],
                          "categoryLabels": [{"properties": {"fontSize": lit(10)}}]})


def header(page: Page) -> None:
    textbox(page, 16, 4, 980, 68, [(page.title, {"fontSize": "16pt", "fontWeight": "bold", "color": "#13304A"}),
                                   (page.subtitle, {"fontSize": "9pt", "color": "#5B6B7B"})])
    page.add("card", 1036, 8, 220, 62, {"Values": [mea("DataFreshness", "Data As Of")]},
             objects={"labels": [{"properties": {"fontSize": lit(12)}}],
                      "categoryLabels": [{"properties": {"fontSize": lit(8)}}]})


S, O, I = "FactSales", "FactOrdersMonthly", "FactInventory"
COUNTRY, CHANNEL = col("DimCountry", "Country"), col("DimChannel", "Channel")
YM = col("DimDate", "Year Month")
LABELS_ON = {"labels": [{"properties": {"show": lit(True)}}]}


def pages() -> list[Page]:
    sales = Page("sales", "Sales performance",
                 "Fulfilled orders by local fulfilment date. EUR at the fulfilment-date rate. Source: ETL analytics.")
    header(sales)
    slicer(sales, 24, 420, col("DimDate", "Date"), "Fulfilment date", "Between")
    slicer(sales, 456, 240, COUNTRY, "Country")
    slicer(sales, 708, 240, CHANNEL, "Channel")
    cards(sales, [mea(S, "Gross Revenue EUR"), mea(S, "Net Revenue EUR"), mea(S, "Fulfilled Orders"),
                  mea(S, "Average Order Value EUR"), mea(S, "Units Sold"), mea(S, "Units per Order")])
    sales.add("lineChart", 24, 248, 760, 228, {"Category": [YM], "Y": [mea(S, "Gross Revenue EUR")],
                                               "Series": [COUNTRY]},
              "Gross revenue (EUR) by month and country", sort=(YM, "Ascending"))
    sales.add("clusteredBarChart", 796, 248, 460, 228, {"Category": [COUNTRY], "Y": [mea(S, "Gross Revenue EUR")]},
              "Gross revenue (EUR) by country", objects=LABELS_ON)
    sales.add("clusteredColumnChart", 24, 488, 460, 220,
              {"Category": [COUNTRY], "Y": [mea(S, "Average Order Value EUR")], "Series": [CHANNEL]},
              "Average order value (EUR) by country and channel", objects=LABELS_ON)
    sales.add("tableEx", 496, 488, 760, 220,
              {"Values": [COUNTRY, CHANNEL, mea(S, "Fulfilled Orders"), mea(S, "Units Sold"),
                          mea(S, "Gross Revenue EUR"), mea(S, "Net Revenue EUR"), mea(S, "Average Order Value EUR"),
                          mea(S, "Units per Order"), mea(S, "Share of Gross Revenue")]},
              "Country and channel comparison")

    orders = Page("orders", "Order performance",
                  "All orders by local placement month and current status. Source: ETL analytics.")
    header(orders)
    slicer(orders, 24, 300, YM, "Placement month")
    slicer(orders, 336, 240, COUNTRY, "Country")
    slicer(orders, 588, 240, CHANNEL, "Channel")
    cards(orders, [mea(O, "Orders Placed"), mea(O, "Fulfilment Rate"), mea(O, "Cancellation Rate"),
                   mea(O, "Payment Failure Rate"), mea(O, "Orders Cancelled"), mea(O, "Orders Payment Failed")])
    orders.add("columnChart", 24, 248, 760, 228,
               {"Category": [YM], "Y": [mea(O, "Orders Placed")], "Series": [col(O, "status")]},
               "Orders placed by month and status", sort=(YM, "Ascending"))
    orders.add("lineChart", 796, 248, 460, 228,
               {"Category": [YM], "Y": [mea(O, "Cancellation Rate"), mea(O, "Payment Failure Rate")]},
               "Cancellation and payment-failure rate by month", sort=(YM, "Ascending"))
    orders.add("pivotTable", 24, 488, 620, 220,
               {"Rows": [COUNTRY], "Columns": [col(O, "status")], "Values": [mea(O, "Orders Placed")]},
               "Orders by country and status")
    orders.add("clusteredBarChart", 656, 488, 600, 220,
               {"Category": [COUNTRY], "Y": [mea(O, "Fulfilment Rate")], "Series": [CHANNEL]},
               "Fulfilment rate by country and channel", objects=LABELS_ON)

    inv = Page("inventory", "Inventory management",
               "Current stock per location and SKU (latest ETL publication). Low stock: available <= reorder point.")
    header(inv)
    slicer(inv, 24, 240, COUNTRY, "Country")
    slicer(inv, 276, 240, col(I, "location_type"), "Location type")
    slicer(inv, 528, 240, col(I, "category"), "Category")
    slicer(inv, 780, 240, col(I, "stock_status"), "Stock status")
    cards(inv, [mea(I, "Units On Hand"), mea(I, "Units Available"), mea(I, "Units Reserved"),
                mea(I, "Stock Positions"), mea(I, "Low Stock Positions"), mea(I, "Out of Stock Positions")])
    inv.add("clusteredBarChart", 24, 248, 400, 228,
            {"Category": [COUNTRY], "Y": [mea(I, "Units On Hand"), mea(I, "Units Available")]},
            "Units on hand and available by country", objects=LABELS_ON)
    inv.add("clusteredBarChart", 436, 248, 400, 228,
            {"Category": [col(I, "location_code")], "Y": [mea(I, "Units Available")]},
            "Units available by location")
    inv.add("donutChart", 848, 248, 408, 228, {"Category": [col(I, "stock_status")], "Y": [mea(I, "Stock Positions")]},
            "Stock positions by status")
    inv.add("tableEx", 24, 488, 1232, 220,
            {"Values": [COUNTRY, (*col(I, "location_code"), "Location"), (*col(I, "sku"), "SKU"), (*col(I, "category"), "Category"),
                        (*mea(I, "Low Stock Available"), "Available"), (*mea(I, "Low Stock Reorder Point"), "Reorder point"),
                        (*mea(I, "Reorder Shortfall Units"), "Shortfall (units)")]},
            "Low-stock positions (largest shortfall first)", sort=(mea(I, "Reorder Shortfall Units"), "Descending"))
    return [sales, orders, inv]


THEME = {
    "name": "NordicCart",
    "dataColors": ["#1F5F8B", "#2A9D8F", "#E9A23B", "#C44E52", "#6C5B7B", "#8AA1B1", "#55A868", "#F28E2B"],
    "background": "#FFFFFF", "foreground": "#1F2A37", "tableAccent": "#1F5F8B",
    "good": "#2A9D8F", "neutral": "#E9A23B", "bad": "#C44E52",
    "textClasses": {"title": {"fontFace": "Segoe UI Semibold", "fontSize": 11, "color": "#13304A"},
                    "callout": {"fontFace": "Segoe UI Semibold", "fontSize": 22, "color": "#13304A"},
                    "label": {"fontFace": "Segoe UI", "fontSize": 9, "color": "#5B6B7B"}},
    "visualStyles": {
        "page": {"*": {"background": [{"color": {"solid": {"color": "#F3F5F8"}}, "transparency": 0}]}},
        "*": {"*": {"background": [{"show": True, "color": {"solid": {"color": "#FFFFFF"}}, "transparency": 0}],
                    "border": [{"show": True, "color": {"solid": {"color": "#DDE3EA"}}, "radius": 8}],
                    "padding": [{"top": 8, "bottom": 8, "left": 10, "right": 10}]}},
        "textbox": {"*": {"background": [{"show": False}], "border": [{"show": False}]}},
    },
}


def write_report(root: Path) -> None:
    d = root / f"{NAME}.Report"
    if d.exists():
        shutil.rmtree(d)
    (d / "definition" / "pages").mkdir(parents=True)
    dump(d / "definition.pbir", {"$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
                                 "version": "4.0",
                                 "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    dump(d / "definition" / "version.json",
         {"$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "definition" / "report.json", {
        "$schema": f"{SCHEMA}/item/report/definition/report/3.3.0/schema.json",
        "themeCollection": {
            "baseTheme": {"name": BASE_THEME, "reportVersionAtImport": IMPORT_VERSION, "type": "SharedResources"},
            "customTheme": {"name": "NordicCart.json", "reportVersionAtImport": IMPORT_VERSION, "type": "RegisteredResources"}},
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": BASE_THEME, "path": f"BaseThemes/{BASE_THEME}.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "NordicCart.json", "path": "NordicCart.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "allowChangeFilterTypes": True, "useEnhancedTooltips": True,
                     "useDefaultAggregateDisplayName": True}})
    install = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop).InstallLocation"],
                             capture_output=True, text=True).stdout.strip()
    theme_src = Path(install) / "bin/WebView2Resources/minerva/sharedresources/BaseThemes" / f"{BASE_THEME}.json"
    if not install or not theme_src.exists():
        raise SystemExit(f"Power BI Desktop base theme {BASE_THEME} not found (is Power BI Desktop installed?)")
    base = d / "StaticResources" / "SharedResources" / "BaseThemes"
    base.mkdir(parents=True)
    shutil.copyfile(theme_src, base / f"{BASE_THEME}.json")
    reg = d / "StaticResources" / "RegisteredResources"
    reg.mkdir(parents=True)
    dump(reg / "NordicCart.json", THEME)
    ps = pages()
    dump(d / "definition" / "pages" / "pages.json",
         {"$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.1.0/schema.json",
          "pageOrder": [p.name for p in ps], "activePageName": ps[0].name})
    for p in ps:
        pd = d / "definition" / "pages" / p.name
        dump(pd / "page.json", {"$schema": f"{SCHEMA}/item/report/definition/page/2.1.0/schema.json",
                                "name": p.name, "displayName": p.title, "displayOption": "FitToPage",
                                "height": 720, "width": 1280})
        for v in p.visuals:
            dump(pd / "visuals" / v["name"] / "visual.json", v)


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--local", action="store_true", help="use this clone's output/etl/analytics (do not commit)")
    g.add_argument("--analytics-dir", type=Path, help="any analytics folder")
    args = p.parse_args()
    if args.local:
        args.analytics_dir = REPO / "output" / "etl" / "analytics"
    elif args.analytics_dir is None:
        args.analytics_dir = PORTABLE_ANALYTICS_DIR
    write_model(HERE, args.analytics_dir.resolve())
    write_report(HERE)
    dump(HERE / f"{NAME}.pbip", {"$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
                                 "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
                                 "settings": {"enableAutoRecovery": True}})
    text(HERE / ".gitignore", "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n")
    doc = ["# NordicCart Power BI: DAX measures", "",
           "Generated by `powerbi/build_pbip.py` from the same specification as the model (do not edit by hand).", ""]
    for folder in dict.fromkeys(m[4] for m in MEASURES):
        doc += [f"## {folder}", "", "| Measure | Table | Format | Meaning | DAX |", "|---|---|---|---|---|"]
        doc += [f"| {m} | {t} | `{fmt}` | {desc} | `{dax.replace('|', '¦')}` |"
                for t, m, dax, fmt, f, desc in MEASURES if f == folder]
        doc.append("")
    text(HERE / "MEASURES.md", "\n".join(doc))
    print(f"written: {HERE / (NAME + '.pbip')} ({len(MEASURES)} measures, {len(TABLES)} tables, "
          f"{len(RELATIONSHIPS)} relationships, {sum(len(p.visuals) for p in pages())} visuals)")


if __name__ == "__main__":
    main()
