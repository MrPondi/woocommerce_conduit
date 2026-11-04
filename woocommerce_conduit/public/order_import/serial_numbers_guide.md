# Converting serial number records for the WooCommerce Order Import

You are helping a person turn their company's earlier records of which serial number was sold with which order into the one CSV file that ERPNext's WooCommerce Order Import accepts. Write a script that reads their records and writes that file. Use Python with only its standard library, unless the person asks for something else. Don't convert the rows by hand in the conversation: a script can be run again on corrected records, and checked.

## What the file is for

The import brings old WooCommerce orders into ERPNext. When "Make Delivery Notes" is ticked, each order gets a Delivery Note dated when it was completed, and the order's serial numbers are put on it. ERPNext then records each serial as delivered to the order's customer, and starts its warranty on the day of the delivery.

The import is strict on purpose. If a single row of the file is wrong, the import refuses the whole file and lists the rows it refused. So the script must check everything below before it writes the file.

## The file, exactly

- Plain text in UTF-8. A byte-order mark at the start is accepted.
- Comma-separated, with standard CSV quoting, which is only needed when a value contains a comma or a quote.
- The first row is exactly:

  ```
  order_number,serial_no
  ```

- Then one row per serial number, with two cells. Neither may be empty, spaces don't count as content, and nothing else goes on the row. There are no blank rows.
- `order_number` is the order's number as WooCommerce shows it, without "#". The order wp-admin lists as "#1234" is `1234`.
- `serial_no` is exactly as ERPNext spells it in its Serial No list: the same letters, the same case, and no spaces around it.
- A serial number appears only once in the whole file.
- An order with three units of a serialized product has three rows, one for each unit's serial.

An example:

```
order_number,serial_no
1234,KS-2024-00017
1234,KS-2024-00018
1240,HX9921
```

## What the import checks

Make the same checks in the script, so the file passes the first time.

1. **On saving the file**, each serial must:
   - be a Serial No in ERPNext;
   - have the status Active;
   - be in the warehouse that the shop's orders ship from;
   - belong to the shop's company.
2. **Also on saving**, no serial may appear twice.
3. **During the import**, for each order, the number of serials of each Item must equal the units of that Item on the order. A serial must not be of an Item the order doesn't contain. An order with a serialized Item and no serials in the file isn't imported.
4. **During the import**, each serial must have come into stock (the Serial No's posting date) on or before the day its order was completed. A delivery can't take a serial that wasn't in stock yet.

Rows of orders that are already in ERPNext are left out, and that is no error. A file can therefore be attached again after part of an import.

An order that fails check 3 or 4 isn't imported at all, and the next run of the import tries it again. The person can correct the file and run it again.

## Ask the person for

1. **Their records.** Ask for the file or files, and a sample of a few rows with the column names. Ask what a column means when it isn't clear, for example whether a date is the sale or the delivery.
2. **An export of ERPNext's Serial Nos.** It is made from the Serial No list, through Menu → Export or the Report view. It needs the columns `serial_no` (the name), `item_code`, `status`, `warehouse` and `posting_date`.
3. **The warehouse the shop's orders ship from**, which is the Warehouse field of the WooCommerce Server.
4. **Optionally, an export of the shop's completed orders.** For each order it needs the number, the date it was completed, and each line's SKU or product with its quantity. With it, the script can make checks 3 and 4 as well.

## How to convert

- **Order numbers.** Normalise them: strip "#", spaces, and any prefix that the old records add to them. Ask the person which prefixes there are.
- **Several serials in one cell.** When a cell holds several serial numbers, split them into rows of their own. They may be separated by commas, semicolons, slashes or line breaks.
- **Stray characters.** Trim each value, and remove invisible characters such as non-breaking or zero-width spaces.
- **Spelling.** Match each serial against the ERPNext export regardless of case, and write it as ERPNext spells it.
- **Order.** Keep the rows in the order of the records.

## Never guess

- Write each row that can't be mapped cleanly to a separate `rejects.csv` file instead of the output, with its original values and the reason. That covers:
  - a serial that isn't in the ERPNext export, isn't Active, or is in another warehouse;
  - a serial that appears twice;
  - an order number that can't be read;
  - a count that doesn't match the order.
- Never invent a serial, correct a typing error on your own, or move a serial to another order. The person decides those, in their records.
- End with a summary:
  - how many rows were read, written and rejected, with the rejected ones by reason;
  - how many orders the output covers.

## What the output is

The CSV file goes into the Serial Numbers field of the WooCommerce Order Import, with "Make Delivery Notes" ticked. Saving the import checks the file again and lists any row that it refuses.
