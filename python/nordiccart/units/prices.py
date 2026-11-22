"""Price unit of work P1 (S6, design 7.1 rank 1): one product's new list price in one market.

    change_price  in ONE transaction: close the current open-ended product_prices row at the change
                  time (valid_to, the only price column a writer may update, V008) and insert the new
                  row from that time. The EXCLUDE constraint of V004 keeps the periods without overlap;
                  the guarded UPDATE (list price and open end as planned) makes a repeat or a changed
                  database a mismatch, never a second change.
"""

from __future__ import annotations

from .base import Ids, expect


def change_price(conn, ids: Ids, change, label: str) -> str:
    """`change` has sku, market, currency, at, old_price, new_price, unit_cost (activity.engine.PriceChange)."""
    product = ids.product[change.sku]
    n = conn.execute("UPDATE product_prices SET valid_to = %s WHERE product_id = %s AND country_code = %s "
                     "AND valid_to IS NULL AND list_price = %s AND unit_cost = %s AND valid_from < %s",
                     (change.at, product, change.market, change.old_price, change.unit_cost, change.at)).rowcount
    expect(n == 1, f"{label}: the current {change.market} price of {change.sku} is not {change.old_price}")
    conn.execute("INSERT INTO product_prices (product_id, country_code, currency_code, list_price, unit_cost, valid_from) "
                 "VALUES (%s, %s, %s, %s, %s, %s)",
                 (product, change.market, change.currency, change.new_price, change.unit_cost, change.at))
    return f"{change.sku} {change.market} {change.old_price} -> {change.new_price} {change.currency}"
