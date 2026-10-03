# Structured-data processing
CSV processing preserves strings and leading zeros:
```python
import csv

with open("input.csv", newline="", encoding="utf-8-sig") as source:
    reader = csv.DictReader(source)
    fields = reader.fieldnames
    assert fields is not None and "amount" in fields
    with open("output.csv", "w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        count = 0
        for row in reader:
            writer.writerow(row)
            count += 1
with open("output.csv", newline="", encoding="utf-8") as result:
    assert sum(1 for _ in csv.DictReader(result)) == count
```
Use decimal.Decimal for exact decimal calculations and specify date/time assumptions.
JSONL can be streamed line by line with json.loads; validate each record and preserve
null/boolean/numeric types when writing json.dumps. Never eval data or execute macros.
The inspector bounds file size at 32 MiB, selected units at 100 and text per unit at 4k
characters. Larger files require a task-specific streaming script, not raising limits
until the entire dataset enters context. Truncated units need a targeted script to inspect
further within that row/page; next_offset advances to the next unit, not omitted text.
